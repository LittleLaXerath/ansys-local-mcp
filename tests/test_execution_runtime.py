from __future__ import annotations

import json
import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = str(PROJECT)

from ansys_mcp.command_registry import WindowsBatchCommand
from ansys_mcp.elevation import ElevatedRequestRejected, ElevatedRequestTimeout, ElevatedSession, ElevationError
from ansys_mcp.jobs import _job_dir, _write_json, cancel_workflow, get_workflow_status, pending_observations, start_workflow
from ansys_mcp.process_supervisor import (
    CancellationToken, WorkflowCancelled, cancellation_context, process_identity, process_identity_matches,
    run_process, terminate_owned_process,
)
from ansys_mcp.runtime import ansys_python_executable


class ExecutionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = PROJECT / ".ansys_mcp" / ("execution-tests-" + uuid.uuid4().hex)
        self.scratch.mkdir(parents=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_cancel_terminates_owned_process_and_keeps_output(self) -> None:
        token = CancellationToken()
        timer = threading.Timer(0.3, token.cancel)
        timer.start()
        try:
            with cancellation_context(token):
                result = run_process(
                    [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
                    cwd=PROJECT, env=os.environ.copy(), timeout_seconds=30,
                    run_dir=self.scratch, log_name="cancel",
                )
        finally:
            timer.cancel()
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertIn("started", result.stdout_tail)
        self.assertFalse(process_identity_matches(result.identity))

    def test_observer_failure_does_not_turn_completed_work_into_a_retryable_launch_error(self) -> None:
        def broken_observer(identity, active):
            raise PermissionError("metadata unavailable")
        token = CancellationToken(on_process=broken_observer)
        with cancellation_context(token):
            result = run_process([sys.executable, "-c", "print('exactly-once')"], cwd=PROJECT,
                env=os.environ.copy(), timeout_seconds=10, run_dir=self.scratch, log_name="observer")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout_tail.count("exactly-once"), 1)
        self.assertTrue(token.observer_errors)

    def test_pid_reuse_identity_is_rejected(self) -> None:
        current = process_identity(os.getpid())
        self.assertIsNotNone(current)
        self.assertTrue(process_identity_matches(current))
        forged = dict(current, creation_time="not-the-creation-time")
        self.assertFalse(process_identity_matches(forged))
        self.assertFalse(terminate_owned_process(forged))
        self.assertTrue(process_identity_matches(current))

    @unittest.skipUnless(os.name == "nt", "Windows job object ownership")
    def test_parent_exit_terminates_its_owned_background_children(self) -> None:
        child_pid_file = self.scratch / "child.pid"
        script = self.scratch / "launcher.py"
        script.write_text(
            "import pathlib,subprocess,sys\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n", encoding="utf-8",
        )
        result = run_process([sys.executable, str(script), str(child_pid_file)], cwd=PROJECT, env=os.environ.copy(), timeout_seconds=10, run_dir=self.scratch, log_name="child-exit")
        self.assertEqual(result.returncode, 0, result.stderr_tail)
        child_pid = int(child_pid_file.read_text())
        deadline = time.monotonic() + 3
        while process_identity(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(process_identity(child_pid), "Owned background process survived closing its Job Object.")

    @unittest.skipUnless(os.name == "nt", "cmd.exe quoting")
    def test_batch_paths_spaces_unicode_and_arguments_arrive_unchanged(self) -> None:
        folder = self.scratch / "batch launcher space \u6d4b\u8bd5"
        folder.mkdir()
        script = folder / "capture.py"
        script.write_text("import json,sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
        batch = folder / "launcher test.bat"
        batch.write_text('@"%ANSYS_MCP_TEST_PYTHON%" "%~dp0capture.py" %*\n', encoding="ascii")
        args = ["a path with spaces.txt", "ordinary", "", "trailing\\", "folder with spaces\\"]
        env = os.environ.copy()
        env["ANSYS_MCP_TEST_PYTHON"] = sys.executable
        result = run_process(WindowsBatchCommand(batch, args), cwd=PROJECT, env=env, timeout_seconds=10, run_dir=self.scratch, log_name="batch")
        self.assertEqual(result.returncode, 0, result.stderr_tail)
        self.assertEqual(json.loads(result.stdout_tail.strip()), args)

    def test_batch_rejects_shell_injection(self) -> None:
        for value in ("hello & whoami", "hello%PATH%", 'hello"world', "a\ncommand"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WindowsBatchCommand(Path("launcher.bat"), [value])


class DurableJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_ids: list[str] = []
        self.scratch = PROJECT / ".ansys_mcp" / ("job-tests-" + uuid.uuid4().hex)
        self.scratch.mkdir(parents=True)

    def tearDown(self) -> None:
        for run_id in self.run_ids:
            status = cancel_workflow(run_id, grace_seconds=0.2)
            deadline = time.monotonic() + 5
            while status.get("worker_alive") and time.monotonic() < deadline:
                time.sleep(0.05)
                status = get_workflow_status(run_id)
            shutil.rmtree(_job_dir(run_id), ignore_errors=True)
            result = status.get("result", {})
            if result.get("run_dir"):
                shutil.rmtree(result["run_dir"], ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def test_job_result_persists_across_client_instances(self) -> None:
        result = start_workflow(workflow={"version": "1", "workdir": str(PROJECT), "steps": [{"type": "assert", "path": str(PROJECT)}]})
        self.assertIn("run_id", result, result)
        self.run_ids.append(result["run_id"])
        deadline = time.monotonic() + 15
        while result.get("status") not in {"complete", "failed", "interrupted"} and time.monotonic() < deadline:
            time.sleep(0.1)
            result = get_workflow_status(result["run_id"])
        self.assertEqual(result["status"], "complete", result)
        self.assertTrue(result["result"]["ok"])
        self.assertNotEqual(result["run_id"], result["workflow_run_id"])
        # A fresh interpreter sees the same durable result without the Popen registry.
        code = "import json,sys; from ansys_mcp.jobs import get_workflow_status; print(json.dumps(get_workflow_status(sys.argv[1])))"
        recovered = subprocess.run([sys.executable, "-c", code, result["run_id"]], cwd=PROJECT, capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(recovered.stdout)["status"], "complete")

    def test_stale_running_manifest_is_never_reported_alive_or_replayed(self) -> None:
        run_id = uuid.uuid4().hex
        self.run_ids.append(run_id)
        directory = _job_dir(run_id)
        directory.mkdir(parents=True)
        identity = dict(process_identity(os.getpid()), creation_time="stale")
        _write_json(directory / "job.json", {"run_id": run_id, "status": "running", "worker": identity})
        result = get_workflow_status(run_id)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["outcome"], "unknown")
        self.assertFalse(result["worker_alive"])
        self.assertFalse((directory / "request.json").exists())

    def test_job_id_cannot_escape_catalog(self) -> None:
        self.assertFalse(get_workflow_status("../../somewhere")["ok"])

    def test_unknown_result_observes_same_request_and_never_completes_workflow(self) -> None:
        run_id = uuid.uuid4().hex
        self.run_ids.append(run_id)
        directory = _job_dir(run_id)
        directory.mkdir(parents=True)
        summary = {"ok": False, "status": "unknown", "steps": [{"id": "model", "type": "mechanical", "status": "unknown", "outputs": {"session": {"id": "same-session"}, "request_id": "same-request"}}]}
        _write_json(directory / "result.json", summary)
        _write_json(directory / "job.json", {"run_id": run_id, "status": "unknown", "worker": None,
            "result_path": "result.json", "pending_observations": pending_observations(summary)})
        with patch("ansys_mcp.mechanical.mechanical_request_status", return_value={"ok": True, "status": "running"}) as observe:
            result = get_workflow_status(run_id)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["worker_alive"])
            observe.assert_called_once_with("same-session", "same-request")
        with patch("ansys_mcp.mechanical.mechanical_request_status", return_value={"ok": True, "status": "complete"}) as observe:
            result = get_workflow_status(run_id)
            self.assertEqual(result["status"], "needs_attention")
            self.assertEqual(result["original_result"]["status"], "unknown")
            self.assertTrue(result["workflow_resume_required"])
            observe.assert_called_once_with("same-session", "same-request")
        # A later controller restart need not retain the product worker after
        # a verified final observation has been durably recorded.
        with patch("ansys_mcp.mechanical.mechanical_request_status") as observe:
            self.assertEqual(get_workflow_status(run_id)["status"], "needs_attention")
            observe.assert_not_called()

    def test_unknown_without_observation_handle_is_preserved(self) -> None:
        run_id = uuid.uuid4().hex
        self.run_ids.append(run_id)
        directory = _job_dir(run_id)
        directory.mkdir(parents=True)
        _write_json(directory / "job.json", {"run_id": run_id, "status": "unknown", "worker": None})
        result = get_workflow_status(run_id)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("No product observation handle", result["observation_reason"])

    def test_unknown_job_cancellation_routes_only_to_original_actor(self) -> None:
        run_id = uuid.uuid4().hex
        self.run_ids.append(run_id)
        directory = _job_dir(run_id)
        directory.mkdir(parents=True)
        handle = {"step_id": "model", "type": "mechanical", "worker_id": "owned-worker", "worker_request_id": "same-request"}
        _write_json(directory / "job.json", {"run_id": run_id, "status": "unknown", "worker": None, "pending_observations": [handle]})
        with patch("ansys_mcp.mechanical.mechanical_elevated_request_status", return_value={"ok": True, "status": "running"}), \
                patch("ansys_mcp.mechanical.cancel_mechanical_request", return_value={"ok": True, "status": "cancel_requested"}) as cancel, \
                patch("ansys_mcp.jobs.terminate_owned_process") as terminate:
            result = cancel_workflow(run_id, grace_seconds=0.2)
            self.assertEqual(result["status"], "cancel_requested")
            cancel.assert_called_once_with(worker_id="owned-worker", worker_request_id="same-request", grace_seconds=0.2)
            terminate.assert_not_called()
        # Teardown need not recover a deliberately synthetic actor.
        _write_json(directory / "job.json", {"run_id": run_id, "status": "needs_attention", "worker": None})

    @unittest.skipUnless(ansys_python_executable().is_file(), "Optional bundled Python integration; no solver is launched")
    def test_cancelling_job_stops_owned_script_without_replay(self) -> None:
        marker = self.scratch / "started.json"
        script = self.scratch / "long.py"
        script.write_text("import json,os,pathlib,time\npathlib.Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':os.getpid()}))\ntime.sleep(60)\n", encoding="utf-8")
        result = start_workflow(workflow={"version": "1", "workdir": str(PROJECT), "steps": [{"type": "python_script", "script_path": str(script)}]})
        self.run_ids.append(result["run_id"])
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), get_workflow_status(result["run_id"]))
        script_identity = process_identity(json.loads(marker.read_text())["pid"])
        self.assertIsNotNone(script_identity)
        cancel_workflow(result["run_id"])
        while time.monotonic() < deadline:
            result = get_workflow_status(result["run_id"])
            if result["status"] in {"cancelled", "failed", "interrupted"}:
                break
            time.sleep(0.05)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse(process_identity_matches(script_identity))

    @unittest.skipUnless(ansys_python_executable().is_file(), "Optional bundled Python integration; no solver is launched")
    def test_worker_outlives_the_client_which_started_it(self) -> None:
        script = self.scratch / "short.py"
        script.write_text("import time;time.sleep(1);print('finished',flush=True)", encoding="utf-8")
        workflow = {"version": "1", "workdir": str(PROJECT), "steps": [{"type": "python_script", "script_path": str(script)}]}
        request_file = self.scratch / "workflow.json"
        request_file.write_text(json.dumps(workflow), encoding="utf-8")
        code = "import json,sys;from ansys_mcp.jobs import start_workflow;print(json.dumps(start_workflow(workflow_path=sys.argv[1])))"
        client = subprocess.run([sys.executable, "-c", code, str(request_file)], cwd=PROJECT, capture_output=True, text=True, check=True, timeout=10)
        result = json.loads(client.stdout)
        self.run_ids.append(result["run_id"])
        deadline = time.monotonic() + 15
        while result["status"] not in {"complete", "failed", "interrupted"} and time.monotonic() < deadline:
            time.sleep(0.1)
            result = get_workflow_status(result["run_id"])
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["result"]["steps"][0]["returncode"], 0)


class ElevationSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = PROJECT / ".ansys_mcp" / ("elevation-tests-" + uuid.uuid4().hex)
        self.session: ElevatedSession | None = None

    def tearDown(self) -> None:
        if self.session:
            try:
                self.session.close()
                deadline = time.monotonic() + 5
                while process_identity_matches(self.session._ready["worker"]) and time.monotonic() < deadline:
                    time.sleep(0.05)
                terminate_owned_process(self.session._ready["worker"])
            except ElevationError:
                pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def launch(self) -> ElevatedSession:
        # Exercise the actual transport without requesting a UAC dialog. This
        # installed pure-JSON handler is safe and requires no Ansys product.
        self.session = ElevatedSession.launch("ansys_mcp.jobs", "_failure", state_dir=self.directory, elevated=False, timeout_seconds=10)
        return self.session

    def test_authenticated_worker_can_be_reattached(self) -> None:
        session = self.launch()
        result = session.request("TEST", {"sample": True}, timeout_seconds=5)
        self.assertEqual(result["diagnostics"][0]["code"], "TEST")
        reattached = ElevatedSession.attach(self.directory)
        self.assertTrue(reattached.summary()["ok"])
        # Simulate restarting the MCP controller: only on-disk credentials and
        # OS creation identity are available to this new interpreter.
        code = "import json,sys;from pathlib import Path;from ansys_mcp.elevation import ElevatedSession;print(json.dumps(ElevatedSession.attach(Path(sys.argv[1])).summary()))"
        recovered = subprocess.run([sys.executable, "-c", code, str(self.directory)], cwd=PROJECT, capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(recovered.stdout)["worker"], session._ready["worker"])

    def test_remote_address_is_rejected_before_connecting(self) -> None:
        session = self.launch()
        original = session._ready["address"]
        session._ready["address"] = ["example.invalid", 12345]
        try:
            with patch("ansys_mcp.elevation.Client") as connect, self.assertRaises(ElevationError):
                session.summary()
            connect.assert_not_called()
        finally:
            session._ready["address"] = original

    def test_post_submission_handler_failure_keeps_original_request_id(self) -> None:
        self.session = ElevatedSession.launch("ansys_mcp.jobs", "_read_json", state_dir=self.directory, elevated=False, timeout_seconds=10)
        with self.assertRaises(ElevationError) as raised:
            self.session.request("TEST", {})  # This selected fixture requires one argument.
        self.assertTrue(raised.exception.request_id)
        self.assertEqual(self.session.query(raised.exception.request_id)["status"], "failed")

    def test_explicit_request_rejection_is_distinct_from_unknown_submission(self) -> None:
        session = self.launch()
        with patch.object(session, "_exchange", return_value={"ok": False, "error": "not submitted"}), self.assertRaises(ElevatedRequestRejected) as raised:
            session.request("TEST", {})
        self.assertIsNone(raised.exception.request_id)

    def test_request_timeout_can_be_queried_without_replay(self) -> None:
        session = self.launch()
        with self.assertRaises(ElevatedRequestTimeout) as raised:
            session.request("TEST", {"sample": True}, timeout_seconds=0)
        request_id = raised.exception.request_id
        deadline = time.monotonic() + 5
        result = session.query(request_id)
        while result["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            result = session.query(request_id)
        self.assertEqual(result["status"], "complete")
        duplicate = session._exchange({"command": "execute", "request_id": request_id, "operation": "TEST", "payload": {}})
        self.assertFalse(duplicate["ok"])

    def test_wrong_authentication_is_rejected_and_session_survives(self) -> None:
        session = self.launch()
        from multiprocessing import AuthenticationError
        from multiprocessing.connection import Client
        with self.assertRaises(AuthenticationError):
            Client(tuple(session._ready["address"]), authkey=b"wrong-key")
        self.assertTrue(session.summary()["ok"])

    @unittest.skipUnless(ansys_python_executable().is_file(), "Optional bundled Python integration; no solver is launched")
    def test_authenticated_cancel_acts_inside_worker_and_preserves_unowned_process(self) -> None:
        self.session = ElevatedSession.launch("ansys_mcp.workflow", "run_workflow", state_dir=self.directory, elevated=False, timeout_seconds=10)
        marker = self.directory / "started.json"
        script = self.directory / "request.py"
        script.write_text("import json,os,pathlib,time\npathlib.Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':os.getpid()}))\ntime.sleep(60)\n", encoding="utf-8")
        workflow = {"version": "1", "workdir": str(PROJECT), "steps": [{"type": "python_script", "script_path": str(script)}]}
        request_id = uuid.uuid4().hex
        # The private transport forwards two JSON arguments to the chosen
        # installed handler. Here run_workflow consumes a recipe and None,
        # exercising exactly the real cancellation context without a solver.
        acknowledgement = self.session._exchange({"command": "execute", "request_id": request_id, "operation": workflow, "payload": None})
        self.assertTrue(acknowledgement["ok"])
        unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            identity = process_identity(json.loads(marker.read_text())["pid"])
            self.assertTrue(self.session.cancel(request_id, grace_seconds=0.5)["ok"])
            while time.monotonic() < deadline:
                result = self.session.query(request_id)
                if result["status"] in {"complete", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            self.assertEqual(result["status"], "cancelled", result)
            self.assertFalse(process_identity_matches(identity))
            self.assertIsNone(unrelated.poll(), "Cancelling an execution request stopped an unowned process.")
            self.assertFalse(self.session.cancel(request_id)["ok"])
        finally:
            # This Popen handle owns the unrelated test process. Its redirector
            # descendants are cleaned with taskkill only in this test fixture.
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(unrelated.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                unrelated.kill()
            unrelated.wait(timeout=5)

    @unittest.skipUnless(ansys_python_executable().is_file(), "Optional bundled Python integration; no solver is launched")
    def test_controller_cancellation_is_forwarded_through_ipc(self) -> None:
        self.session = ElevatedSession.launch("ansys_mcp.workflow", "run_workflow", state_dir=self.directory, elevated=False, timeout_seconds=10)
        script = self.directory / "request.py"
        script.write_text("import time;time.sleep(60)", encoding="utf-8")
        workflow = {"version": "1", "workdir": str(PROJECT), "steps": [{"type": "python_script", "script_path": str(script)}]}
        token = CancellationToken()
        timer = threading.Timer(1, token.cancel)
        timer.start()
        try:
            with cancellation_context(token), self.assertRaises(WorkflowCancelled) as raised:
                self.session.request(workflow, None, timeout_seconds=10)
        finally:
            timer.cancel()
        request_id = raised.exception.request_id
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.session.query(request_id)
            if result["status"] in {"cancelled", "failed", "complete"}:
                break
            time.sleep(0.05)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse(result["owned_processes"])


class StdioLifetimeTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real SDK's kill-on-close Windows Job, not just Popen exit."""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().set_debug(False)
        self.scratch = PROJECT / ".ansys_mcp" / ("stdio-tests-" + uuid.uuid4().hex)
        self.scratch.mkdir(parents=True)
        self.actor_directory = self.scratch / "actor"
        self.run_id = None
        self.errlog = (self.scratch / "stdio.log").open("a", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        if self.actor_directory.exists():
            try:
                actor = ElevatedSession.attach(self.actor_directory)
                actor.close()
                deadline = time.monotonic() + 5
                while process_identity_matches(actor._ready["worker"]) and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
            except ElevationError:
                pass
        if self.run_id:
            status = cancel_workflow(self.run_id, grace_seconds=0.2)
            deadline = time.monotonic() + 5
            while status.get("worker_alive") and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                status = get_workflow_status(self.run_id)
            if status.get("result", {}).get("run_dir"):
                shutil.rmtree(status["result"]["run_dir"], ignore_errors=True)
            shutil.rmtree(_job_dir(self.run_id), ignore_errors=True)
        self.errlog.close()
        shutil.rmtree(self.scratch, ignore_errors=True)

    @asynccontextmanager
    async def connect(self, server: Path | None = None):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        environment = dict(os.environ, ANSYS_MCP_ALLOWED_ROOTS=str(PROJECT))
        environment["PYTHONPATH"] = str(PROJECT) + os.pathsep + environment.get("PYTHONPATH", "")
        parameters = StdioServerParameters(command=sys.executable, args=[str(server or PROJECT / "ansys_mcp_server.py")], cwd=str(PROJECT), env=environment)
        async with stdio_client(parameters, errlog=self.errlog) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize()
                yield session

    def data(self, packet):
        self.assertFalse(packet.isError, packet)
        return packet.structuredContent or json.loads(next(item.text for item in packet.content if item.type == "text"))

    async def test_job_survives_real_stdio_server_close_and_reconnect(self) -> None:
        proof = self.scratch / "proof.txt"
        async with self.connect() as client:
            result = self.data(await client.call_tool("start_workflow", {"workflow": {"version": "1", "workdir": str(PROJECT), "steps": [{"id": "proof", "type": "file", "action": "write_text", "path": str(proof), "content": "once"}]}}))
            self.assertTrue(result["ok"], result)
            self.run_id = result["run_id"]
        # Exiting stdio_client closes its kill-on-close Job Object on Windows.
        # An ordinary Popen child would now be gone despite a new process group.
        async with self.connect() as client:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                result = self.data(await client.call_tool("get_workflow_status", {"run_id": self.run_id}))
                if result["status"] in {"complete", "failed", "interrupted"}:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(result["status"], "complete", result)
            self.assertEqual(proof.read_text(), "once")

    async def _actor_survives_real_stdio_server_close(self, elevated: bool) -> None:
        server = self.scratch / "actor_server.py"
        server.write_text(
            "from pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\n"
            "from ansys_mcp.elevation import ElevatedSession\napp=FastMCP('lifetime-test')\n"
            "@app.tool()\ndef start_actor(state_dir:str,elevated:bool=False)->dict:\n"
            "    return ElevatedSession.launch('ansys_mcp.jobs','_failure',state_dir=Path(state_dir),elevated=elevated).summary()\n"
            "@app.tool()\ndef query_actor(state_dir:str)->dict:\n"
            "    return ElevatedSession.attach(Path(state_dir)).request('TEST',{'same_actor':True})\n"
            "app.run()\n", encoding="utf-8",
        )
        async with self.connect(server) as client:
            result = self.data(await client.call_tool("start_actor", {"state_dir": str(self.actor_directory), "elevated": elevated}))
            identity = result["worker"]
        self.assertTrue(process_identity_matches(identity), "The actor inherited the MCP client's kill-on-close Job.")
        async with self.connect(server) as client:
            result = self.data(await client.call_tool("query_actor", {"state_dir": str(self.actor_directory)}))
            self.assertEqual(result["diagnostics"][0]["code"], "TEST")
            self.assertEqual(ElevatedSession.attach(self.actor_directory)._ready["worker"], identity)
            self.assertEqual(ElevatedSession.attach(self.actor_directory)._ready["elevated"], elevated)

    async def test_authenticated_actor_survives_real_stdio_server_close(self) -> None:
        await self._actor_survives_real_stdio_server_close(False)

    @unittest.skipUnless(os.name == "nt" and os.environ.get("ANSYS_MCP_RUN_ELEVATION_TESTS") == "1", "Explicit opt-in required for Windows UAC lifecycle test")
    async def test_elevated_actor_survives_real_stdio_server_close(self) -> None:
        await self._actor_survives_real_stdio_server_close(True)


if __name__ == "__main__":
    unittest.main()
