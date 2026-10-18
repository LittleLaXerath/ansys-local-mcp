from __future__ import annotations

import csv
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp.dpf import (
    _execute_recipe, _export, _resolve_input, _start_server, _summary,
    dpf_produced_paths, format_capabilities, run_dpf_recipe, validate_dpf_recipe_paths,
)
from ansys_mcp.dpf_schema import DPFRecipe
from ansys_mcp.process_supervisor import ProcessResult
from ansys_mcp.workflow_paths import PathChecks


class FakeField:
    location = "ElementalNodal"
    unit = "Pa"
    component_count = 2
    scoping = types.SimpleNamespace(ids=[23, 11])

    def get_entity_data(self, index):
        return [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0]]][index]


class FakeContainer:
    def __len__(self):
        return 1

    def __getitem__(self, index):
        return FakeField()

    def get_label_space(self, index):
        return {"time": 2, "complex": 0}


class DPFAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_strict_recipe_rejects_ambiguous_and_forward_references(self):
        for data in (
            {"operations": [{"op": "metadata", "id": "meta"}]},
            {"operations": [{"op": "export", "id": "out", "source": "later", "path": "out.csv"}]},
            {"operations": [{"op": "operator", "id": "calc", "name": "norm_fc", "inputs": {"0": {"$ref": "missing"}}}]},
            {"server": {"transport": "grpc", "insecure": True}, "operations": [{"op": "discover", "id": "readers"}]},
            {"operations": [{"op": "discover", "id": "same"}, {"op": "discover", "id": "same"}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                DPFRecipe.model_validate(data)

    def test_paths_are_checked_before_starting_any_server(self):
        recipe = DPFRecipe.model_validate({"result_files": [{"path": "../outside.rst"}],
            "operations": [{"op": "metadata", "id": "meta"}]})
        with self.assertRaises(ValueError), patch("ansys_mcp.dpf._load_dpf") as load:
            run_dpf_recipe(recipe, self.root, self.root / "run", 5)
        load.assert_not_called()

    def test_schema_mode_accepts_a_declared_future_result(self):
        recipe = DPFRecipe.model_validate({"result_files": [{"path": "future.rst"}],
            "operations": [{"op": "result", "id": "u", "name": "displacement"},
                           {"op": "export", "id": "csv", "source": "u", "path": "out.csv"}]})
        validate_dpf_recipe_paths(recipe, self.root, path_checks=PathChecks(self.root, mode="schema"))
        checks = PathChecks(self.root)
        checks.declare("future.rst")
        validate_dpf_recipe_paths(recipe, self.root, path_checks=checks)
        self.assertEqual(dpf_produced_paths(recipe), ["out.csv"])

    def test_export_preserves_labels_units_entity_ids_and_element_node_rows(self):
        csv_path = self.root / "fields.csv"
        artifact = _export(FakeContainer(), csv_path, "csv", False)
        self.assertEqual(artifact["rows"], 3)
        with csv_path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["entity_id"] for row in rows], ["23", "23", "11"])
        self.assertEqual([row["entity_row"] for row in rows], ["0", "1", "0"])
        self.assertEqual(rows[0]["unit"], "Pa")
        self.assertEqual(json.loads(rows[0]["label_space"]), {"time": 2, "complex": 0})
        json_path = self.root / "fields.json"
        _export(FakeContainer(), json_path, "json", False)
        fields = json.loads(json_path.read_text(encoding="utf-8"))["fields"]
        self.assertEqual(fields[0]["entities"][0], {"id": 23, "data": [[1.0, 2.0], [3.0, 4.0]]})
        self.assertEqual(fields[0]["location"], "ElementalNodal")

    def test_existing_export_is_preserved_and_no_partial_file_remains(self):
        target = self.root / "existing.csv"
        target.write_text("original", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            _export(FakeField(), target, "csv", False)
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_summaries_do_not_transfer_whole_field_data(self):
        value = _summary(FakeContainer(), sample_entities=1)
        self.assertEqual(value["field_count"], 1)
        self.assertEqual(value["fields"][0]["sample"][0]["entity_id"], 23)
        self.assertNotIn("data", value["fields"][0])

    def test_summary_uses_scoping_size_and_indexed_ids_without_bulk_transfer(self):
        class LargeScoping:
            size = 10000000

            @property
            def ids(self):
                raise AssertionError("A summary must not fetch all node IDs.")

            def id(self, index):
                return 23 + index

        field = FakeField()
        field.scoping = LargeScoping()
        self.assertEqual(_summary(field)["fields"][0]["entities"], 10000000)
        self.assertEqual(_summary(field, 1)["fields"][0]["sample"][0]["entity_id"], 23)

    def test_grpc_never_inherits_an_insecure_environment_default(self):
        captured = {}
        fake = types.SimpleNamespace(
            server_factory=types.SimpleNamespace(CommunicationProtocols=types.SimpleNamespace(InProcess="InProcess", gRPC="gRPC"),
                                                 GrpcMode=types.SimpleNamespace(mTLS="mtls")),
            ServerConfig=lambda **kwargs: kwargs,
            start_local_server=lambda **kwargs: captured.update(kwargs) or object(),
        )
        with patch.dict(os.environ, {"DPF_DEFAULT_GRPC_MODE": "insecure"}):
            _start_server(fake, DPFRecipe.model_validate({"server": {"transport": "grpc"},
                "operations": [{"id": "readers", "op": "discover"}]}), 10)
        self.assertEqual(captured["config"]["grpc_mode"], "mtls")
        self.assertEqual(captured["ip"], "127.0.0.1")
        self.assertFalse(captured["as_global"])
        self.assertFalse(captured["use_pypim_by_default"])

    def test_generic_operator_inputs_cannot_smuggle_file_paths_as_strings(self):
        with self.assertRaises(ValueError):
            _resolve_input("../outside.rst", {}, self.root, "file_path")
        with self.assertRaises(ValueError):
            _resolve_input({"$path": "../outside.rst", "mode": "write"}, {}, self.root, "file_path")
        self.assertEqual(_resolve_input("Nodal", {}, self.root, "requested_location"), "Nodal")
        value = object()
        self.assertIs(_resolve_input({"$ref": "u"}, {"u": value}, self.root, "fields_container"), value)

    def test_only_own_server_is_shut_down_on_failure(self):
        shutdown = []
        fake_server = types.SimpleNamespace(shutdown=lambda: shutdown.append("owned"))
        with patch("ansys_mcp.dpf._load_dpf", return_value=types.SimpleNamespace(available_operator_names=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("reader failure")))), \
             patch("ansys_mcp.dpf._start_server", return_value=fake_server):
            result = _execute_recipe(DPFRecipe.model_validate({"operations": [{"id": "readers", "op": "discover"}]}), self.root, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(shutdown, ["owned"])
        self.assertEqual(result["diagnostics"][0]["operation_id"], "readers")

    def test_cancelled_and_timed_out_workers_do_not_report_partial_success(self):
        recipe = {"operations": [{"id": "readers", "op": "discover"}]}
        for cancelled, timed_out, status in ((True, False, "cancelled"), (False, True, "timed_out")):
            process = ProcessResult(1, 123, timed_out, False, self.root / "out.log", self.root / "err.log", "", "", 1,
                                    cancelled=cancelled)
            with patch("ansys_mcp.dpf.run_process", return_value=process):
                result = run_dpf_recipe(recipe, self.root, self.root / "run", 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], status)

    def test_child_preserves_parent_implicit_root_when_workdir_is_a_subdirectory(self):
        workdir = self.root / "nested"
        workdir.mkdir()
        process = ProcessResult(1, 123, True, False, self.root / "out.log", self.root / "err.log", "", "", 1)
        with patch.dict(os.environ, {}, clear=False), patch("ansys_mcp.config.STARTUP_CWD", self.root), \
             patch("ansys_mcp.dpf.run_process", return_value=process) as run:
            os.environ.pop("ANSYS_MCP_ALLOWED_ROOTS", None)
            run_dpf_recipe({"operations": [{"id": "readers", "op": "discover"}]}, workdir, workdir / "run", 1)
            self.assertEqual(run.call_args.kwargs["env"]["ANSYS_MCP_ALLOWED_ROOTS"], str(self.root))

    def test_autodyn_support_is_not_inferred_from_ls_dyna_readers(self):
        rows = {row["component"]: row for row in format_capabilities(["lsdyna::d3plot::S", "mapdl::rst::U"])}
        self.assertTrue(rows["ls_dyna"]["runtime_reachable"])
        self.assertFalse(rows["ls_dyna"]["file_read_verified"])
        self.assertFalse(rows["autodyn"]["runtime_reachable"])


if __name__ == "__main__":
    unittest.main()
