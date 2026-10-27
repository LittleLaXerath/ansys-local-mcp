from __future__ import annotations

import os
import time
import unittest
from pathlib import Path

from ansys_mcp.cfx_recipe import (
    ERROR_LOG_MISSING, ERROR_LOG_STALE, ERROR_OUTPUT_MISSING, ERROR_OUTPUT_STALE,
    ERROR_RESULT_MISSING, ERROR_RESULT_PATH_MISMATCH, ERROR_RESULT_STALE,
    ERROR_RESULT_UNDECLARED, ERROR_SOLVER_INCOMPLETE, cfx_produced_paths, run_cfx_recipe,
    validate_cfx_recipe_paths,
)
from ansys_mcp.cfx_schema import CFXRecipe
from ansys_mcp.process_supervisor import ProcessResult
from ansys_mcp.workflow import validate_workflow
from ansys_mcp.workflow_paths import PathChecks, PathDeniedError

PROJECT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ANSYS_MCP_ALLOWED_ROOTS", str(PROJECT))
os.chdir(PROJECT)

SOLVER_LOG = """\
 Total Time = 5.0000E-03 [s]
 Residual Target = 1e-5
 Residual Type = RMS
 TIME STEP =     1 SIMULATION TIME = 2.5000E-03 CPU SECONDS = 1.0E+02
 COEFFICIENT LOOP ITERATION =    1              CPU SECONDS = 1.0E+02
 | U-Mom                | 0.10 | 4.0E-06 | 1.0E-04 |       1.0E-02  OK|
 | P-Mass               | 0.20 | 5.0E-07 | 2.0E-06 |  9.9  5.0E-02  OK|
 TIME STEP =     2 SIMULATION TIME = 5.0000E-03 CPU SECONDS = 2.0E+02
 COEFFICIENT LOOP ITERATION =    1              CPU SECONDS = 2.0E+02
 | U-Mom                | 0.10 | 3.0E-06 | 9.0E-05 |       1.0E-02  OK|
 | P-Mass               | 0.20 | 4.0E-07 | 1.0E-06 |  9.9  5.0E-02  OK|
 This run of the ANSYS CFX Solver has finished.
"""


class FakeRunner:
    def __init__(self, effect=None):
        self.calls: list[dict] = []
        self.effect = effect

    def __call__(self, argv, *, cwd, env, timeout_seconds, run_dir, log_name, supervise_handoff=False):
        self.calls.append({"argv": list(argv), "cwd": Path(cwd), "timeout": timeout_seconds,
                           "log_name": log_name, "env": env, "supervise_handoff": supervise_handoff})
        if self.effect is not None:
            self.effect(Path(cwd), Path(run_dir), log_name)
        stdout = Path(run_dir) / f"{log_name}.stdout.log"
        stderr = Path(run_dir) / f"{log_name}.stderr.log"
        stdout.write_text("fake stdout\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return ProcessResult(returncode=0, pid=1234, timed_out=False, detached=False,
                             stdout_path=stdout, stderr_path=stderr, stdout_tail="fake stdout",
                             stderr_tail="", elapsed_s=0.01, handoff_supervised=supervise_handoff)


class CFXRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = PROJECT / ".ansys_mcp" / "cfx-tests" / (str(int(time.time() * 1000)) + "-" + str(id(self)))
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.pre = self.root / "setup.pre"
        self.pre.write_text("FLOW: Flow Analysis 1\nEND\n", encoding="utf-8")
        self.def_path = self.root / "model.def"
        self.def_path.write_text("domain", encoding="utf-8")
        self.res_path = self.root / "model_001.res"
        self.post_script = self.root / "post.cse"
        self.post_script.write_text("! post", encoding="utf-8")

    def recipe(self, operation: dict) -> CFXRecipe:
        return CFXRecipe.model_validate({"operation": operation})

    def _run(self, recipe: CFXRecipe, runner: FakeRunner) -> dict:
        return run_cfx_recipe(recipe, workdir=self.root, run_dir=self.run_dir,
                              timeout_seconds=60, runner=runner, log_name="cfx-test")

    def _stale(self, path: Path) -> None:
        old = time.time() - 3600
        os.utime(path, (old, old))

    # -- pre -----------------------------------------------------------------
    def test_pre_completes_when_a_fresh_def_is_written(self) -> None:
        def write_def(cwd, run_dir, log_name):
            (cwd / "model.def").write_text("new", encoding="utf-8")

        runner = FakeRunner(write_def)
        result = self._run(self.recipe({"op": "pre", "script": "setup.pre", "def_path": "model.def"}), runner)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["command"]["id"], "cfx_pre")
        self.assertEqual(runner.calls[0]["argv"][1:], ["-batch", str(self.pre)])
        self.assertEqual(result["outputs"]["def_path"], str(self.def_path))

    def test_pre_missing_or_stale_def_is_a_failure(self) -> None:
        missing = self._run(self.recipe({"op": "pre", "script": "setup.pre", "def_path": "absent.def"}), FakeRunner())
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["diagnostics"][0]["code"], ERROR_OUTPUT_MISSING)

        self._stale(self.def_path)
        stale = self._run(self.recipe({"op": "pre", "script": "setup.pre", "def_path": "model.def"}), FakeRunner())
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["diagnostics"][0]["code"], ERROR_OUTPUT_STALE)

    # -- solve ---------------------------------------------------------------
    def test_solve_builds_argv_and_reads_monitor(self) -> None:
        def write_outputs(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("result", encoding="utf-8")
            (cwd / "model_001.out").write_text(SOLVER_LOG, encoding="utf-8")

        runner = FakeRunner(write_outputs)
        recipe = self.recipe({"op": "solve", "def_path": "model.def", "res_path": "model_001.res",
                              "run_name": "model", "precision": "double", "partitions": 4,
                              "start_method": "Intel MPI Local Parallel"})
        result = self._run(recipe, runner)
        self.assertTrue(result["ok"], result)
        argv = runner.calls[0]["argv"][1:]
        self.assertEqual(argv[:2], ["-def", str(self.def_path)])
        for flag in ("-double", "-part", "4", "-par-local", "-start-method", "Intel MPI Local Parallel", "-name", "model"):
            self.assertIn(flag, argv)
        monitor = result["outputs"]["monitor"]
        self.assertEqual(monitor["time_steps"], 2)
        self.assertEqual(monitor["last_simulation_time_s"], 5e-3)
        self.assertTrue(monitor["finished_marker"])
        self.assertIs(monitor["end_time_reached"], True)
        self.assertTrue(result["outputs"]["completion"]["finished_marker"])
        self.assertTrue(runner.calls[0]["supervise_handoff"])
        self.assertTrue(result["process"]["handoff_supervised"])

    def test_incomplete_solver_log_with_fresh_result_is_incomplete(self) -> None:
        def partial_log(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("result", encoding="utf-8")
            (cwd / "model_001.out").write_text(
                " Total Time = 5 [s]\n TIME STEP =     1 SIMULATION TIME = 5.0000E-04 CPU SECONDS = 1.0E+02\n",
                encoding="utf-8")

        runner = FakeRunner(partial_log)
        recipe = self.recipe({"op": "solve", "def_path": "model.def", "res_path": "model_001.res",
                              "run_name": "model"})
        result = self._run(recipe, runner)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "incomplete")
        self.assertIn(ERROR_SOLVER_INCOMPLETE, {item["code"] for item in result["diagnostics"]})
        self.assertIs(result["outputs"]["completion"]["finished_marker"], False)
        self.assertIs(result["outputs"]["completion"]["result_fresh"], True)

    def test_undeclared_result_keeps_freshness_unknown(self) -> None:
        def finished_log(cwd, run_dir, log_name):
            (cwd / "model_001.out").write_text(SOLVER_LOG, encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def", "run_name": "model"}),
                           FakeRunner(finished_log))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "incomplete")
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn(ERROR_RESULT_UNDECLARED, codes)
        self.assertIsNone(result["outputs"]["completion"]["result_fresh"])
        self.assertIs(result["outputs"]["completion"]["result_declared"], False)

    def test_stale_completion_log_without_declared_result_is_not_complete(self) -> None:
        old_log = self.root / "model_001.out"
        old_log.write_text(SOLVER_LOG, encoding="utf-8")
        self._stale(old_log)
        result = self._run(self.recipe({"op": "solve", "def_path": "model.def", "run_name": "model"}),
                           FakeRunner())
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "incomplete")
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn(ERROR_LOG_STALE, codes)

    def test_stale_log_with_fresh_result_is_not_complete(self) -> None:
        old_log = self.root / "model_001.out"
        old_log.write_text(SOLVER_LOG, encoding="utf-8")
        self._stale(old_log)

        def fresh_result(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("result", encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                        "res_path": "model_001.res", "run_name": "model"}), FakeRunner(fresh_result))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "incomplete")
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn(ERROR_LOG_STALE, codes)
        self.assertIs(result["outputs"]["completion"]["log_fresh"], False)

    def test_zero_byte_result_is_rejected(self) -> None:
        def empty_result(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("", encoding="utf-8")
            (cwd / "model_001.out").write_text(SOLVER_LOG, encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                        "res_path": "model_001.res", "run_name": "model"}), FakeRunner(empty_result))
        self.assertEqual(result["status"], "failed")
        self.assertIn(ERROR_RESULT_STALE, {item["code"] for item in result["diagnostics"]})

    def test_log_result_path_must_match_the_declared_result(self) -> None:
        def mismatched(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("result", encoding="utf-8")
            (cwd / "model_001.out").write_text(SOLVER_LOG + " D:\\cases\\other_002.res\n", encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                        "res_path": "model_001.res", "run_name": "model"}), FakeRunner(mismatched))
        self.assertEqual(result["status"], "incomplete")
        self.assertIn(ERROR_RESULT_PATH_MISMATCH, {item["code"] for item in result["diagnostics"]})

    def test_declared_result_missing_is_distinct_from_incomplete(self) -> None:
        def only_log(cwd, run_dir, log_name):
            (cwd / "model_001.out").write_text(SOLVER_LOG, encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                       "res_path": "model_001.res", "run_name": "model"}), FakeRunner(only_log))
        self.assertEqual(result["status"], "failed")
        self.assertIn(ERROR_RESULT_MISSING, {item["code"] for item in result["diagnostics"]})

    def test_stale_result_cannot_be_reported_as_new(self) -> None:
        self.res_path.write_text("old", encoding="utf-8")
        self._stale(self.res_path)

        def write_log(cwd, run_dir, log_name):
            (cwd / "model_001.out").write_text(SOLVER_LOG, encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                       "res_path": "model_001.res", "run_name": "model"}), FakeRunner(write_log))
        self.assertEqual(result["status"], "failed")
        self.assertIn(ERROR_RESULT_STALE, {item["code"] for item in result["diagnostics"]})

    def test_end_time_reached_without_marker_is_complete_with_warning(self) -> None:
        def outputs(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("result", encoding="utf-8")
            (cwd / "model_001.out").write_text(SOLVER_LOG.replace("This run of the ANSYS CFX Solver has finished.", ""),
                                               encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                       "res_path": "model_001.res", "run_name": "model"}), FakeRunner(outputs))
        self.assertTrue(result["ok"], result)
        codes = {item["code"] for item in result["diagnostics"]}
        self.assertIn("CFX_FINISH_MARKER_MISSING", codes)
        self.assertTrue(all(item["severity"] == "warning" for item in result["diagnostics"]))

    def test_missing_log_with_fresh_result_is_incomplete(self) -> None:
        def outputs(cwd, run_dir, log_name):
            (cwd / "model_001.res").write_text("result", encoding="utf-8")

        result = self._run(self.recipe({"op": "solve", "def_path": "model.def",
                                       "res_path": "model_001.res", "out_path": "declared.out"}), FakeRunner(outputs))
        self.assertEqual(result["status"], "incomplete")
        self.assertIn(ERROR_LOG_MISSING, {item["code"] for item in result["diagnostics"]})

    # -- post ----------------------------------------------------------------
    def test_post_requires_fresh_declared_outputs(self) -> None:
        def write_output(cwd, run_dir, log_name):
            (cwd / "image.png").write_text("png", encoding="utf-8")

        recipe = self.recipe({"op": "post", "script": "post.cse",
                              "outputs": [{"name": "image", "path": "image.png"}]})
        result = self._run(recipe, FakeRunner(write_output))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["command"]["id"], "cfx_post")

        missing = self._run(self.recipe({"op": "post", "script": "post.cse",
                                        "outputs": [{"name": "image", "path": "absent.png"}]}), FakeRunner())
        self.assertEqual(missing["status"], "failed")
        self.assertIn(ERROR_OUTPUT_MISSING, {item["code"] for item in missing["diagnostics"]})

    # -- policy and workflow integration ------------------------------------
    def test_paths_outside_allowed_roots_are_denied(self) -> None:
        checks = PathChecks(self.root, mode="schema")
        with self.assertRaises(PathDeniedError):
            validate_cfx_recipe_paths(self.recipe({"op": "pre", "script": str(PROJECT.parent / "outside.pre")}),
                                      self.root, path_checks=checks)

    def test_workflow_schema_accepts_cfx_and_rejects_unknown_fields_and_retries(self) -> None:
        base = {"version": "1", "workdir": str(self.root), "steps": [
            {"id": "pre", "type": "cfx",
             "recipe": {"operation": {"op": "pre", "script": "setup.pre", "def_path": "model.def"}}}]}
        self.assertTrue(validate_workflow(workflow=base, validation_mode="schema")["ok"])

        unknown = {**base, "steps": [{**base["steps"][0],
                   "recipe": {"operation": {"op": "pre", "script": "setup.pre", "rms_target": 1e-5}}}]}
        self.assertFalse(validate_workflow(workflow=unknown, validation_mode="schema")["ok"])

        retry = {**base, "steps": [{**base["steps"][0], "retry": {"max_attempts": 2}}]}
        self.assertFalse(validate_workflow(workflow=retry, validation_mode="schema")["ok"])

    def test_produced_paths_follow_the_operation(self) -> None:
        self.assertEqual(cfx_produced_paths(self.recipe({"op": "pre", "script": "setup.pre", "def_path": "model.def"})),
                         ["model.def"])
        self.assertEqual(cfx_produced_paths(self.recipe({"op": "post", "script": "post.cse",
                                                        "outputs": [{"name": "a", "path": "a.csv"}]})), ["a.csv"])


if __name__ == "__main__":
    unittest.main()

