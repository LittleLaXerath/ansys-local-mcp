from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ansys_mcp import fluent_session as fs
from ansys_mcp.elevation import ElevatedRequestTimeout
from ansys_mcp.schemas import PyFluentRecipe, PyFluentStep
from ansys_mcp.workflow import validate_workflow, run_workflow
from ansys_mcp.process_supervisor import CancellationToken, cancellation_context
from ansys_mcp.process_supervisor import WorkflowCancelled


PROJECT = Path(__file__).resolve().parents[1]


class FluentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=PROJECT)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        self.env.start()
        self.sid = "a" * 32

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def recipe(self, action="new", **kwargs):
        return PyFluentRecipe.model_validate({"mode": "persistent", "session": {
            "action": action, **({"id": self.sid} if action != "new" else {})}, **kwargs})

    def actor(self):
        actor = fs.FluentActor(self.sid, "bundled", self.root)
        actor.solver = MagicMock()
        actor.solver.monitors.get_monitor_set_names.return_value = []
        actor.state = "ready"
        return actor

    def payload(self, action="reuse", **kwargs):
        return fs.prepare_recipe(self.recipe(action, **kwargs), self.root)

    def record(self, **kwargs):
        path = fs._record_path(self.sid, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fs._write_json(path, {"id": self.sid, "state": "complete", "runtime": "bundled",
                            "state_dir": str(self.root), "worker": {"pid": 1}, **kwargs})
        return path

    def test_strict_launch_rejects_unknown_and_unsafe_options(self):
        for launch in ({"processor_counts": 4}, {"processor_count": 0}, {"processor_count": True},
                       {"additional_arguments": "-i outside.jou"}, {"env": {}},
                       {"dimension": 4}, {"mode": "unknown"}, {"fluent_path": "other.exe"}):
            with self.subTest(launch=launch), self.assertRaises(ValueError):
                self.recipe(launch=launch)

    def test_session_contract(self):
        for payload in ({"mode": "batch", "session": {"action": "reuse", "id": self.sid}},
                        {"mode": "persistent", "session": {"action": "new", "id": self.sid}},
                        {"mode": "persistent", "session": {"action": "reuse"}},
                        {"mode": "persistent", "session": {"action": "reuse", "id": "../bad"}},
                        {"mode": "persistent", "session": {"action": "reuse", "id": self.sid}, "launch": {}},
                        {"mode": "persistent", "session": {"action": "close", "id": self.sid}, "operations": [{"op": "calculate"}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                PyFluentRecipe.model_validate(payload)

    def test_schema_validation_never_launches_and_rejects_retry(self):
        base = {"version": "1", "workdir": str(self.root), "steps": [{"type": "pyfluent", "mode": "persistent"}]}
        with patch.object(fs.ElevatedSession, "launch") as launch:
            self.assertTrue(validate_workflow(workflow=base, validation_mode="schema")["ok"])
            base["steps"][0]["retry"] = {"max_attempts": 2}
            self.assertFalse(validate_workflow(workflow=base, validation_mode="schema")["ok"])
            launch.assert_not_called()

    def test_paths_relative_to_workdir_and_rechecked_before_execution(self):
        (self.root / "input.cas.h5").write_bytes(b"case")
        payload = fs.prepare_recipe(self.recipe(read="input.cas.h5", write={"case": "new.cas.h5"}), self.root)
        self.assertEqual(payload["read"]["case"], str(self.root / "input.cas.h5"))
        (self.root / "input.cas.h5").unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            fs.prepare_recipe(self.recipe(read="input.cas.h5"), self.root)
        with self.assertRaises(ValueError):
            fs.prepare_recipe(self.recipe(write={"case": str(PROJECT.parent / "outside.cas")}), self.root)

    def test_generic_file_keyword_cannot_escape_roots(self):
        workflow = {"version": "1", "workdir": str(self.root), "steps": [{"type": "pyfluent", "operations": [
            {"op": "call", "path": "settings.file.read_mesh", "kwargs": {"file_name": str(PROJECT.parent / "outside.msh")}}]}]}
        self.assertFalse(validate_workflow(workflow=workflow, validation_mode="schema")["ok"])

    def test_generation_read_and_mutation_not_reads(self):
        actor = self.actor()
        with patch.object(fs, "_run_operation", return_value=3):
            result = actor.execute(self.payload(operations=[{"op": "get_state", "path": "settings"},
                                                          {"op": "set_state", "path": "settings.x", "value": 1, "store_as": "value"}]))
        self.assertTrue(result["ok"], result)
        self.assertEqual(actor.generation, 1)
        self.assertEqual(result["outputs"]["results"]["value"], 3)
        payload = self.payload()
        payload["session"]["expected_generation"] = 0
        mismatch = actor.execute(payload)
        self.assertEqual(mismatch["diagnostics"][0]["code"], "PYFLUENT_GENERATION_MISMATCH")

    def test_operation_failure_is_structured_and_no_replay(self):
        actor = self.actor()
        with patch.object(fs, "_run_operation", side_effect=RuntimeError("bad state")) as call:
            result = actor.execute(self.payload(operations=[{"op": "calculate"}]))
        self.assertFalse(result["ok"])
        self.assertEqual(actor.generation, 1)
        self.assertEqual(result["outputs"]["operations"][0]["index"], 0)
        call.assert_called_once()

    def test_non_json_object_is_not_silently_stored_as_a_handle(self):
        actor = self.actor()
        with patch.object(fs, "_run_operation", return_value=object()):
            result = actor.execute(self.payload(operations=[{"op": "get_state", "path": "settings"}]))
        self.assertFalse(result["ok"])

    def test_switch_mode_replaces_root_and_increments_generation(self):
        actor = self.actor()
        new_solver = actor.solver.switch_to_solver.return_value
        with patch.object(fs, "load_pyfluent"), patch.object(actor, "_attach_streams"):
            result = actor.execute(self.payload(operations=[{"op": "call", "path": "switch_to_solver"}]))
        self.assertTrue(result["ok"], result)
        self.assertIs(actor.solver, new_solver)
        self.assertEqual(actor.generation, 1)

    def test_monitor_incremental_and_events_preserve_indices(self):
        actor = self.actor()
        actor.solver.monitors.get_monitor_set_names.return_value = ["residual"]
        actor.solver.monitors.get_monitor_set_data.return_value = ([1, 2], {"continuity": [0.1, 0.01]})
        actor._monitor()
        actor._monitor()
        actor._event(event_info=types.SimpleNamespace(index=2))
        lines = [json.loads(line) for line in (self.root / "monitors.jsonl").read_text().splitlines()]
        self.assertEqual([line["index"] for line in lines], [1, 2])
        self.assertEqual(json.loads((self.root / "events.jsonl").read_text())["index"], 2)

    def test_monitor_failure_does_not_invent_convergence(self):
        actor = self.actor()
        actor.solver.monitors.get_monitor_set_names.side_effect = RuntimeError("not initialized")
        actor._monitor()
        self.assertFalse((self.root / "monitors.jsonl").exists())
        self.assertIn("not initialized", (self.root / "monitor-errors.jsonl").read_text())

    def test_close_failure_not_marked_closed(self):
        actor = self.actor()
        actor.solver.exit.side_effect = RuntimeError("lost connection")
        result = actor.execute(self.payload("close"))
        self.assertFalse(result["ok"])
        self.assertEqual(actor.state, "close_failed")

    def test_cancellation_preserves_owned_process_handles_without_replay(self):
        actor = self.actor()
        identity = {"pid": 123456, "creation_time": "fake"}
        actor.owned = [identity]
        token = CancellationToken()
        with cancellation_context(token), patch.object(fs, "_run_operation", side_effect=lambda *args: token.cancel()):
            result = actor.execute(self.payload(operations=[{"op": "calculate"}, {"op": "calculate"}]))
        self.assertFalse(result["ok"])
        self.assertEqual(actor.state, "disconnected")
        self.assertEqual(token.owned_processes(), [identity])

    def test_close_cleans_only_recorded_creation_identities(self):
        actor = self.actor()
        identity = {"pid": 123456, "creation_time": "fake"}
        actor.owned = [identity]
        with patch.object(fs, "process_identity_matches", side_effect=[True, False, False]), patch(
                "ansys_mcp.process_supervisor.terminate_owned_process", return_value=True) as terminate:
            result = actor.execute(self.payload("close"))
        self.assertTrue(result["ok"], result)
        terminate.assert_called_once_with(identity)

    def test_generic_return_selection_handles_arrays_without_persisting_objects(self):
        from ansys_mcp.pyfluent_recipe_runner import _run_operation, _path_tokens
        solver = types.SimpleNamespace(data=lambda: {"fluid": [1, 2]})
        self.assertEqual(_run_operation(solver, {"op": "call", "path": "data", "select": '["fluid"]'}), [1, 2])
        self.assertEqual(_path_tokens("materials['水'].density")[1], ("index", "水"))

    def test_native_protobuf_commands_convert_to_structured_json(self):
        from google.protobuf.struct_pb2 import Struct
        message = Struct()
        message.update({"status": "mesh checked"})
        self.assertEqual(fs._json_value(message), {"status": "mesh checked"})

    def test_channel_mesh_generator_has_expected_topology(self):
        from tools.run_fluent_acceptance import write_channel_mesh
        path = self.root / "mesh.msh"
        write_channel_mesh(path, nx=2, ny=2)
        text = path.read_text()
        self.assertIn('(10 (0 1 9 0 2))', text)
        self.assertIn('(12 (2 1 4 1 3))', text)
        self.assertIn('(13 (0 1 c 0))', text)

    def test_cfd_catalog_is_offline_and_does_not_claim_runtime_paths(self):
        from ansys_mcp.cfd_catalog import _declarations, query_cfd_catalog
        import ast
        tree = ast.parse('class Root:\n    def method(self):\n        raise RuntimeError("must not execute")\n')
        rows = _declarations(tree)
        self.assertEqual([row["qualified_name"] for row in rows], ["Root", "Root.method"])
        result = query_cfd_catalog("cfx", "261", None, None, 0, 10)
        self.assertEqual(len(result["items"]), 3)
        self.assertTrue(all(row["runtime_reachable"] is None for row in result["items"]))

    def test_dead_worker_never_relaunches(self):
        self.record()
        with patch.object(fs.ElevatedSession, "attach", side_effect=RuntimeError("dead")), patch.object(fs.ElevatedSession, "launch") as launch:
            result = fs.run_persistent(self.recipe("reuse"), workdir=self.root, run_dir=self.root, timeout_seconds=1)
        self.assertFalse(result["ok"])
        launch.assert_not_called()

    def test_reconnection_authenticates_original_worker(self):
        self.record()
        proxy = MagicMock()
        proxy.summary.return_value = {"worker": {"pid": 1}}
        proxy.request.return_value = {"ok": True, "status": "complete", "outputs": {"session": {"id": self.sid}}, "diagnostics": []}
        with patch.object(fs.ElevatedSession, "attach", return_value=proxy) as attach, patch.object(fs.ElevatedSession, "launch") as launch:
            result = fs.run_persistent(self.recipe("reuse"), workdir=self.root, run_dir=self.root, timeout_seconds=1)
        self.assertTrue(result["ok"])
        launch.assert_not_called()
        attach.assert_called_once()

    def test_wrong_worker_identity_rejected(self):
        self.record()
        proxy = MagicMock()
        proxy.summary.return_value = {"worker": {"pid": 2}}
        with patch.object(fs.ElevatedSession, "attach", return_value=proxy):
            result = fs.run_persistent(self.recipe("reuse"), workdir=self.root, run_dir=self.root, timeout_seconds=1)
        self.assertFalse(result["ok"])
        proxy.request.assert_not_called()

    def test_timeout_persists_request_before_dispatch_and_only_observes(self):
        path = self.record()
        proxy = MagicMock()
        proxy.summary.return_value = {"worker": {"pid": 1}}
        def timeout(*args, **kwargs):
            self.assertEqual(fs._read_json(path)["request_id"], kwargs["request_id"])
            raise ElevatedRequestTimeout(kwargs["request_id"])
        proxy.request.side_effect = timeout
        with patch.object(fs.ElevatedSession, "attach", return_value=proxy):
            result = fs.run_persistent(self.recipe("reuse"), workdir=self.root, run_dir=self.root, timeout_seconds=1)
            self.assertEqual(result["status"], "unknown")
            proxy.query.return_value = {"status": "running"}
            second = fs.run_persistent(self.recipe("reuse"), workdir=self.root, run_dir=self.root, timeout_seconds=1)
        self.assertEqual(second["status"], "unknown")
        proxy.request.assert_called_once()

    def test_cancel_retains_original_observation_handle(self):
        self.record()
        proxy = MagicMock()
        proxy.summary.return_value = {"worker": {"pid": 1}}
        def cancel(*args, **kwargs):
            exc = WorkflowCancelled("Observe original cancellation.")
            exc.request_id = kwargs["request_id"]
            raise exc
        proxy.request.side_effect = cancel
        with patch.object(fs.ElevatedSession, "attach", return_value=proxy):
            result = fs.run_persistent(self.recipe("reuse"), workdir=self.root, run_dir=self.root, timeout_seconds=1)
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["outputs"]["cancel_requested"])
        self.assertRegex(result["outputs"]["request_id"], r"^[a-f0-9]{32}$")

    def test_multiple_steps_reference_same_session_id(self):
        workflow = {"version": "1", "workdir": str(self.root), "steps": [
            {"type": "pyfluent", "id": "first", "mode": "persistent"},
            {"type": "pyfluent", "id": "next", "mode": "persistent", "session": {
                "action": "reuse", "id": "${steps.first.outputs.session.id}"}}]}
        def run(model, **kwargs):
            if model.session.action == "reuse":
                self.assertEqual(model.session.id, self.sid)
            return {"ok": True, "status": "complete", "outputs": {"session": {"id": self.sid}}, "diagnostics": [], "artifacts": []}
        with patch.object(fs, "run_persistent", side_effect=run), patch.object(fs, "validate_runtime"):
            result = run_workflow(workflow=workflow, timeout_seconds=5)
        self.assertTrue(result["ok"], result)

    def test_interface_counts_and_schema(self):
        from ansys_mcp.server import mcp
        self.assertEqual(len(asyncio.run(mcp.list_tools())), 17)
        resources = asyncio.run(mcp.list_resources())
        self.assertEqual(len(resources), 6)
        self.assertIn("ansys://schema/pyfluent-recipe", {str(r.uri) for r in resources})
        self.assertFalse(PyFluentRecipe.model_json_schema()["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
