from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ansys_mcp.maintenance import apply_retention, retention_plan


PROJECT = Path(__file__).resolve().parents[1]


class RetentionTests(unittest.TestCase):
    def setUp(self):
        parent = PROJECT / ".ansys_mcp"
        parent.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.clock = time.time()

    def run_files(self, name="finished", status="complete", extra=None, group="runs"):
        directory = self.root / ".ansys_mcp" / group / name
        directory.mkdir(parents=True)
        manifest = directory / ("manifest.json" if group == "runs" else "job.json")
        manifest.write_text(json.dumps({"status": status, **(extra or {})}))
        log = directory / "step.attempt-1.stdout.log"
        log.write_text("full process output")
        native = directory / "model.rst"
        native.write_text("must be retained")
        old = self.clock - 40 * 86400
        for path in (manifest, log, native): os.utime(path, (old, old))
        return manifest, log, native

    def test_default_is_read_only_and_apply_deletes_only_selected_log(self):
        manifest, log, native = self.run_files()
        receipt = self.root / ".ansys_mcp" / "capability_evidence" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text("retained evidence")
        plan = retention_plan(self.root, now=self.clock)
        self.assertTrue(plan["dry_run"])
        self.assertTrue(log.is_file())
        self.assertEqual([item["path"] for item in plan["candidates"]], [str(log)])
        applied = apply_retention(plan)
        self.assertTrue(applied["ok"], applied)
        self.assertFalse(log.exists())
        self.assertTrue(native.is_file())
        self.assertTrue(manifest.is_file())
        self.assertTrue(receipt.is_file())

    def test_active_unknown_and_session_runs_are_retained(self):
        logs = []
        for state in ("running", "starting", "unknown", "interrupted", "needs_attention"):
            logs.append(self.run_files(state, state)[1])
        logs.append(self.run_files("session", extra={"outputs": {"session": {"id": "owned"}}})[1])
        self.assertEqual(retention_plan(self.root, now=self.clock)["candidates"], [])
        self.assertTrue(all(path.exists() for path in logs))

    def test_manifest_and_log_changes_invalidate_old_plan(self):
        manifest, log, _ = self.run_files()
        plan = retention_plan(self.root, now=self.clock)
        log.write_text("changed")
        self.assertEqual(apply_retention(plan)["deleted"], [])
        plan = retention_plan(self.root, older_than_days=0)
        manifest.write_text('{"status":"running"}')
        self.assertEqual(apply_retention(plan)["deleted"], [])

    def test_live_worker_and_pending_job_references_protect_logs(self):
        manifest, log, _ = self.run_files()
        job, _, _ = self.run_files("owner", "running", group="jobs")
        (job.parent / "result.json").write_text(json.dumps({"run_dir": str(manifest.parent)}))
        self.assertEqual(retention_plan(self.root, now=self.clock)["candidates"], [])
        job.write_text(json.dumps({"status": "complete", "worker": {"pid": 7, "creation_time": "test"}}))
        with patch("ansys_mcp.maintenance.process_identity_matches", return_value=True):
            self.assertEqual(retention_plan(self.root, now=self.clock)["candidates"], [])
        self.assertTrue(log.is_file())

    def test_forged_candidate_cannot_delete_an_artifact(self):
        _, _, native = self.run_files()
        plan = retention_plan(self.root, now=self.clock)
        plan["candidates"][0]["path"] = str(native)
        self.assertEqual(apply_retention(plan)["deleted"], [])
        self.assertTrue(native.is_file())

    def test_hard_linked_logs_and_new_manifests_are_retained(self):
        manifest, log, _ = self.run_files()
        os.link(log, log.with_name("protected-copy.txt"))
        self.assertEqual(retention_plan(self.root, now=self.clock)["candidates"], [])
        self.run_files("recent")
        recent = self.root / ".ansys_mcp/runs/recent/manifest.json"
        os.utime(recent, (self.clock, self.clock))
        self.assertEqual(retention_plan(self.root, now=self.clock)["candidates"], [])

    def test_linked_run_directory_is_not_followed(self):
        _, _, native = self.run_files()
        with patch("ansys_mcp.maintenance._plain", side_effect=lambda path, **kw: path.name != "finished"):
            self.assertEqual(retention_plan(self.root, now=self.clock)["candidates"], [])
        self.assertTrue(native.exists())


if __name__ == "__main__":
    unittest.main()
