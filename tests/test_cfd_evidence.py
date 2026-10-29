from __future__ import annotations

import copy
import csv
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ansys_mcp import capabilities, cfd_evidence
from ansys_mcp.acceptance_evidence import AcceptanceReceipt, file_sha256, import_receipt, inspect_receipt
from tools.prepare_cfd_receipt import prepare


PROJECT = Path(__file__).resolve().parents[1]


class CFDEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root), "ANSYS_MCP_EVIDENCE_ROOT": str(self.root / "evidence")})
        env.start()
        self.addCleanup(env.stop)
        self.scope = {"installation_fingerprint": "f" * 64, "version": "261", "current_installation": True}
        scoped = patch.object(cfd_evidence, "workflow_scope", return_value=self.scope)
        scoped.start()
        self.addCleanup(scoped.stop)
        prepared_scope = patch("tools.prepare_cfd_receipt.workflow_scope", return_value=self.scope)
        prepared_scope.start()
        self.addCleanup(prepared_scope.stop)
        self.case = self.root / "run"
        self.case.mkdir()
        capabilities._EVIDENCE.clear()
        self.addCleanup(capabilities._EVIDENCE.clear)

    def write(self, name, value):
        path = self.case / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def result(self, outputs, *, state="complete", kind="pyfluent", run_id="fixture_run"):
        return {"ok": True, "run_id": run_id, "steps": [{"type": kind, "ok": True, "status": state, "outputs": outputs}]}

    def fluent(self):
        session = {"id": "a" * 32, "product_version": "Ansys Fluent 2026 R1", "client_version": "0.37.1", "directory": str(self.case)}
        outputs = {"session": session}
        for name in ("01-new", "02-reconnect", "06-close"):
            value = copy.deepcopy(outputs)
            if name == "06-close":
                value["session"].update(state="closed", live_owned_processes=[])
            self.write(name + ".result.json", self.result(value))
        physics = {"models": {"viscous": {"model": "laminar"}},
                   "materials": {"fluid": {"air": {"density": {"value": 1}, "viscosity": {"value": .001}}}},
                   "boundary_conditions": {"velocity_inlet": {"inlet": {"momentum": {"velocity_magnitude": {"value": .1}}}}}}
        self.write("03-solve.result.json", self.result({**outputs, "results": {"physics": physics,
                   "mass_flows": [{"mass_in": [.001, "kg/s"]}, {"mass_out": [-.001, "kg/s"]}]}}))
        xy = [(0.2*(i+.5)/20, .01*(j+.5)/20) for i in range(20) for j in range(20)]
        fields = {"u": [.6*(y/.01)*(1-y/.01) for x, y in xy], "p": [12*(.2-x) for x, y in xy],
                  "centroids": [v for point in xy for v in point]}
        for name in ("04-readback", "05-reopen"):
            self.write(name+".result.json", self.result({**outputs, "results": fields}))
        for name in ("channel.cas.h5", "channel.dat.h5", "channel.msh", "transcript.log", "monitors.jsonl", "events.jsonl"):
            (self.case / name).write_bytes(b"Synthetic fixture only, not real product evidence.")
        self.write("03-solve.workflow.json", {"steps": [{"operations": [{"op": "write", "case_data": str(self.case / "channel.cas.h5")}]}]})
        self.write("05-reopen.workflow.json", {"steps": [{"read": {"case_data": str(self.case / "channel.cas.h5")}}]})
        artifacts = {}
        for name in ("channel.cas.h5", "channel.dat.h5", "channel.msh", "03-solve.result.json", "04-readback.result.json", "05-reopen.result.json"):
            path = self.case / name
            artifacts[name] = {"size": path.stat().st_size, "sha256": file_sha256(path)}
        self.write("numerical-acceptance-v2.json", {"passed": True, "artifacts": artifacts})
        return prepare("fluent", self.case, self.root / "receipt")

    def cfx(self):
        data = self.case / "data"
        data.mkdir()
        for name in ("smoke_001.res", "smoke.def", "restart_001.res", "prepare.pre"):
            (data / name).write_text("Synthetic fixture, not a solver result.")
        result_path = str((data / "smoke_001.res").resolve())
        (data / "export.cse").write_text(">load filename="+result_path.replace("\\", "/")+"\n")
        log = "This run of the CFX 2026 R1 Solver started\nResidual Target = 1e-5\nResidual Type = RMS\n"
        for i in range(1, 5):
            log += f"TIME STEP = {i} SIMULATION TIME = {i*.005} CPU SECONDS = 1\n"
            log += "COEFFICIENT LOOP ITERATION = 1 CPU SECONDS = 1\n"
            log += "| U-Mom | 0.1 | 1e-6 | 1e-5 | 1e-7 OK|\n"
        log += f"| The results from this run of the ANSYS CFX Solver have been |\n| written to |\n| {result_path} |\nThis run of the ANSYS CFX Solver has finished.\n"
        (data / "smoke_001.out").write_text(log)
        self.write("specification.json", {"end_time_s": .02, "source_time_s": 0, "dt_s": .005, "expected_steps": 4})
        geometry = self.write("case_specification.json", {"geometry": {"x_min_m": 0, "x_max_m": 1,
            "y_min_m": 0, "y_max_m": 1, "z_min_m": 0, "z_max_m": .001, "diameter_m": .1},
            "fluid": {"density_kg_m3": 1, "inlet_velocity_m_s": [.1, 0, 0]}})
        reference = self.case / "forces.csv"
        reference.write_text('TIME,"USER POINT,Cylinder_Fx","USER POINT,Cylinder_Fy"\n0.02,1,0.1\n')
        measured = {"time_s": .02, "volume_m3": (1-math.pi*.05**2)*.001, "min_volume_m3": 1e-9,
                    "mass_in_kg_s": .0001, "mass_out_kg_s": -.0001, "force_x_N": 1, "force_y_N": .1}
        with (data / "metrics.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(measured))
            writer.writeheader()
            writer.writerow(measured)
        self.write("acceptance.json", {"ok": True, "measured": {**measured,
                   "relative_mass_imbalance": 0, "force_x_relative_error": 0, "force_y_relative_error": 0}})
        self.executables = {key: self.root / (key+".exe") for key in ("cfx_pre", "cfx_solve", "cfx_post")}
        steps = []
        for op in ("pre", "solve", "post"):
            steps.append({"ok": True, "type": "cfx", "status": "complete", "outputs": {"operation": op},
                          "process": {"returncode": 0, "handoff_supervised": True, "job_processes_at_exit": 0},
                          "command": {"executable": str(self.executables["cfx_"+op])}})
        steps[1]["outputs"].update(res_path=result_path, log_path=str(data / "smoke_001.out"))
        for step, role, path in ((steps[0], "def", data / "smoke.def"), (steps[1], "res", data / "smoke_001.res")):
            step["outputs"]["artifacts"] = [{"role": role, "path": str(path), "fresh": True,
                                            "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}]
        self.write("workflow-result.json", {"ok": True, "run_id": "synthetic_cfx", "steps": steps})
        return prepare("cfx", self.case, self.root / "receipt", reference_specification=geometry, reference_forces=reference)

    def change_bound(self, receipt, key, mutate):
        data = json.loads(receipt.read_text())
        path = Path(data[key]["path"])
        value = json.loads(path.read_text())
        mutate(value)
        path.write_text(json.dumps(value))
        data[key]["sha256"] = file_sha256(path)
        receipt.write_text(json.dumps(data))

    def test_fluent_receipt_dry_run_import_reconnect_query(self):
        receipt = self.fluent()
        dry = import_receipt(receipt)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(Path(dry["index_path"]).exists())
        import_receipt(receipt, apply=True)
        self.assertTrue(import_receipt(receipt, apply=True)["already_imported"])
        old = Path.cwd()
        try:
            os.chdir(self.root)
            result = capabilities.query_capabilities(product="fluent", kind="workflow", version="261")
        finally:
            os.chdir(old)
        item = result["items"][0]
        self.assertEqual(item["status"], "verified")
        self.assertEqual(item["scope_origin"], "historical_revalidation")
        self.assertEqual(item["verification_scope"], "steady_planar_channel_only")
        self.assertEqual(capabilities._EVIDENCE, {})

    def test_changed_native_artifact_revokes_workflow_evidence(self):
        receipt = self.fluent()
        import_receipt(receipt, apply=True)
        (self.case / "channel.dat.h5").write_bytes(b"changed")
        result = capabilities.query_capabilities(product="fluent", kind="workflow", version="261")
        self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_current_receipt_hash_cannot_hide_changed_original_fluent_data(self):
        receipt = self.fluent()
        path = self.case / "channel.dat.h5"
        path.write_bytes(b"half result")
        data = json.loads(receipt.read_text())
        for item in data["artifacts"]:
            if item["path"] == str(path): item["sha256"] = file_sha256(path)
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "original_artifact_manifest"):
            inspect_receipt(receipt)

    def test_saved_passed_true_cannot_override_bad_native_fields(self):
        receipt = self.fluent()
        self.change_bound(receipt, "report", lambda value: value["steps"][0]["outputs"]["results"]["u"].__setitem__(0, 100))
        with self.assertRaisesRegex(ValueError, "numerical"):
            import_receipt(receipt, apply=True)

    def test_changed_index_labels_cannot_borrow_a_valid_receipt(self):
        receipt = self.fluent()
        checked = import_receipt(receipt, apply=True)
        index = Path(checked["index_path"])
        data = json.loads(index.read_text())
        data["receipt"]["case_id"] = "unobserved_case"
        index.write_text(json.dumps(data))
        result = capabilities.query_capabilities(product="fluent", kind="workflow", version="261")
        self.assertEqual(result["items"][0]["status"], "unavailable")

    def test_every_reopened_source_requires_an_explicit_hash(self):
        receipt = self.fluent()
        data = json.loads(receipt.read_text())
        data["artifacts"] = [item for item in data["artifacts"] if not item["path"].endswith("05-reopen.result.json")]
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "hash-bound"):
            inspect_receipt(receipt)

    def test_product_backend_and_validator_are_not_interchangeable(self):
        receipt = self.fluent()
        data = json.loads(receipt.read_text())
        for product, backend, validator in (("mechanical", "pyfluent", "fluent_channel"), ("cfx", "pyfluent", "cfx_short_restart"),
                                            ("fluent", "pyfluent", "contact")):
            with self.subTest(product=product, backend=backend, validator=validator), self.assertRaises(ValueError):
                AcceptanceReceipt.model_validate({**data, "product": product, "backend": backend, "validator": validator})

    def test_cfd_workflow_evidence_does_not_promote_apis(self):
        receipt = self.fluent()
        self.change_bound(receipt, "report", lambda value: value.update(executed_apis=["settings.everything"]))
        with self.assertRaisesRegex(ValueError, "workflow evidence only"):
            import_receipt(receipt, apply=True)
        self.assertEqual(capabilities._EVIDENCE, {})

    def test_malformed_and_nonfinite_reports_are_rejected(self):
        receipt = self.fluent()
        self.change_bound(receipt, "report", lambda value: value["steps"][0]["outputs"]["results"].update(u=[math.nan]*400))
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            inspect_receipt(receipt)

    def test_cfx_short_restart_receipt_is_queryable_but_not_full_regression(self):
        receipt = self.cfx()
        with patch.object(cfd_evidence, "known_paths", return_value=self.executables):
            checked = import_receipt(receipt, apply=True)
            result = capabilities.query_capabilities(product="cfx", kind="workflow", version="261")
        self.assertTrue(checked["checks"]["ok"])
        self.assertEqual(result["items"][0]["verification_scope"], "short_restart_only_not_shedding_frequency")
        self.assertEqual(checked["imported_apis"], [])

    def test_cfx_missing_finish_marker_cannot_borrow_saved_pass(self):
        receipt = self.cfx()
        path = self.case / "data/smoke_001.out"
        path.write_text(path.read_text().replace("This run of the ANSYS CFX Solver has finished.", "interrupted"))
        data = json.loads(receipt.read_text())
        for item in data["artifacts"]:
            if item["path"] == str(path): item["sha256"] = file_sha256(path)
        receipt.write_text(json.dumps(data))
        with patch.object(cfd_evidence, "known_paths", return_value=self.executables), self.assertRaisesRegex(ValueError, "numerical"):
            inspect_receipt(receipt)

    def test_current_receipt_hash_cannot_hide_changed_original_cfx_result(self):
        receipt = self.cfx()
        path = self.case / "data/smoke_001.res"
        path.write_bytes(b"half result")
        data = json.loads(receipt.read_text())
        for item in data["artifacts"]:
            if item["path"] == str(path): item["sha256"] = file_sha256(path)
        receipt.write_text(json.dumps(data))
        with patch.object(cfd_evidence, "known_paths", return_value=self.executables), self.assertRaisesRegex(ValueError, "original_artifact_metadata"):
            inspect_receipt(receipt)

    def test_cfx_zero_flow_malformed_export_fails_with_a_clear_validation_error(self):
        receipt = self.cfx()
        path = self.case / "data/metrics.csv"
        with path.open(newline="") as stream: rows = list(csv.DictReader(stream))
        rows[0]["mass_in_kg_s"] = "0"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        data = json.loads(receipt.read_text())
        for item in data["artifacts"]:
            if item["path"] == str(path): item["sha256"] = file_sha256(path)
        receipt.write_text(json.dumps(data))
        with patch.object(cfd_evidence, "known_paths", return_value=self.executables), self.assertRaisesRegex(ValueError, "Malformed CFD evidence"):
            inspect_receipt(receipt)


class CFDFingerprintTests(unittest.TestCase):
    def test_fingerprints_track_only_the_selected_product_and_binary_changes(self):
        with tempfile.TemporaryDirectory(dir=PROJECT) as temp:
            root = Path(temp)
            binaries = {key: root / (key + ".exe") for key in ("fluent", "cfx_pre", "cfx_solve", "cfx_post")}
            for key, path in binaries.items(): path.write_bytes(key.encode())
            install = SimpleNamespace(root=root, version_tag="261")
            with patch.object(cfd_evidence, "discover_installation", return_value=install), patch.object(cfd_evidence, "known_paths", return_value=binaries):
                fluent = cfd_evidence.workflow_scope("fluent", "261")
                cfx = cfd_evidence.workflow_scope("cfx", "261")
                self.assertNotEqual(fluent["installation_fingerprint"], cfx["installation_fingerprint"])
                binaries["cfx_solve"].write_bytes(b"modified binary; no signature or license check")
                changed = cfd_evidence.workflow_scope("cfx", "261")
                self.assertNotEqual(changed["installation_fingerprint"], cfx["installation_fingerprint"])
                self.assertEqual(cfd_evidence.workflow_scope("fluent", "261"), fluent)
                binaries["cfx_pre"].unlink()
                self.assertFalse(cfd_evidence.workflow_scope("cfx", "261")["current_installation"])
                self.assertFalse(cfd_evidence.workflow_scope("fluent", "252")["current_installation"])


if __name__ == "__main__":
    unittest.main()
