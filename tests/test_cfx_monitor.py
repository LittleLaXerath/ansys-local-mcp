from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from ansys_mcp.cfx_monitor import empty_state, parse_cfx_log, read_cfx_log, summarize

PROJECT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ANSYS_MCP_ALLOWED_ROOTS", str(PROJECT))

LOG_HEADER = """\
 +--------------------------------------------------------------------+
 |                    CFX Solver run information                      |
 +--------------------------------------------------------------------+
 Total Time = 5 [s]
 SIMULATION TYPE: Transient
 CONVERGENCE CRITERIA:
   Residual Target = 1e-5
   Residual Type = RMS
 END
 MONITOR POINT: Cylinder_Cd
 MONITOR POINT: Cylinder_Cl
"""

STEP = """\
 ----------------------------------------------------------------------
 TIME STEP =     1 SIMULATION TIME = 5.0000E-03 CPU SECONDS = 1.564E+02
 ----------------------------------------------------------------------
 COEFFICIENT LOOP ITERATION =    1              CPU SECONDS = 1.564E+02
 ----------------------------------------------------------------------
 |       Equation       | Rate | RMS Res | Max Res |  Linear Solution |
 +----------------------+------+---------+---------+------------------+
 | U-Mom                | 0.00 | 7.2E-03 | 3.2E-01 |       3.6E-02  OK|
 | V-Mom                | 0.00 | 3.2E-05 | 7.0E-04 |       4.6E-01  ok|
 | P-Mass               | 0.00 | 2.9E-03 | 1.5E-01 |  9.1  6.5E-02  OK|
 +----------------------+------+---------+---------+------------------+
 COEFFICIENT LOOP ITERATION =    2              CPU SECONDS = 1.645E+02
 | U-Mom                | 0.66 | 4.7E-06 | 2.4E-04 |       9.6E-03  OK|
 | V-Mom                |37.90 | 1.2E-06 | 6.8E-05 |       2.8E-02  OK|
 | P-Mass               | 0.10 | 2.9E-07 | 2.4E-06 | 13.2  6.5E-02  OK|
 +----------------------+------+---------+---------+------------------+

 ======================================================================
 |                      Timestepping Information                      |
 +----------------------+----------------------+----------------------+
 |       Timestep       |  RMS Courant Number  |  Max Courant Number  |
 +----------------------+----------------------+----------------------+
 |      5.0000E-03      |          2.79        |         12.90        |
 +----------------------+----------------------+----------------------+
"""

STEP2 = """\
 TIME STEP =     2 SIMULATION TIME = 1.0000E-02 CPU SECONDS = 1.952E+02
 COEFFICIENT LOOP ITERATION =    1              CPU SECONDS = 1.952E+02
 | U-Mom                | 0.10 | 4.0E-06 | 1.0E-04 |       1.0E-02  OK|
 | P-Mass               | 0.20 | 5.0E-07 | 2.0E-06 |  9.9  5.0E-02  OK|
"""

TAIL = """\
 +--------------------------------------------------------------------+
 | The results from this run of the ANSYS CFX Solver have been        |
 | written to                                                         |
 | D:\\cases\\model_001.res                                            |
 +--------------------------------------------------------------------+
 This run of the ANSYS CFX Solver has finished.
"""


class CFXMonitorTests(unittest.TestCase):
    def test_result_footer_ignores_restart_input_and_joins_wrapped_path(self):
        text = r" D:\work\restart_001.res" + "\n" + "\n".join([
            " | The results from this run of the ANSYS CFX Solver have been |",
            " | written to |",
            r" | D:\work\case-20261028- |",
            r" | -unique\smoke_001.res |",
            " +------------------+",
        ]) + "\n"
        state = parse_cfx_log(text)
        self.assertEqual(state["result_path"], r"D:\work\case-20261028-unique\smoke_001.res")

    def test_wrapped_result_footer_preserves_spaces_and_incremental_state(self):
        first = "\n".join([
            " | The results from this run of the ANSYS CFX Solver have been |",
            " | written to |", r" | D:\work\a directory |",
        ]) + "\n"
        state = parse_cfx_log(first)
        self.assertIsNone(state["result_path"])
        parse_cfx_log(r" | with spaces\result.res |" + "\n", state)
        self.assertEqual(state["result_path"], r"D:\work\a directory with spaces\result.res")

    def test_parallel_memory_and_other_numeric_tables_are_not_residuals(self):
        text = LOG_HEADER + STEP + "\n".join([
            " | Host Memory Information (Mbytes): Solver |",
            " | Host | Nproc | System | Peak | % |",
            " | WORKSTATION | 4 | 64992.15 | 1256.38 | 1.93 |",
            " | 1.0 | 2.0 | 3.0 |",
        ]) + "\n"
        state = parse_cfx_log(text)
        self.assertEqual(set(state["samples"][-1]["equations"]), {"U-Mom", "V-Mom", "P-Mass"})
        self.assertTrue(summarize(state)["converged"])
        self.assertEqual(state["samples"][-1]["courant"]["max"], 12.9)

    def test_parse_time_steps_equations_and_metadata(self) -> None:
        state = parse_cfx_log(LOG_HEADER + STEP + STEP2 + TAIL)
        self.assertEqual([sample["time_step"] for sample in state["samples"]], [1, 2])
        self.assertEqual(state["samples"][0]["simulation_time_s"], 5e-3)
        self.assertEqual(state["samples"][0]["equations"]["U-Mom"]["rms"], 4.7e-6)
        self.assertEqual(state["samples"][1]["equations"]["P-Mass"]["rms"], 5e-7)
        self.assertEqual(state["residual_target"], 1e-5)
        self.assertEqual(state["residual_type"], "RMS")
        self.assertEqual(state["total_time_s"], 5.0)
        self.assertEqual(state["monitor_points"], ["Cylinder_Cd", "Cylinder_Cl"])
        self.assertTrue(state["finished"])
        self.assertEqual(state["result_path"], r"D:\cases\model_001.res")
        self.assertEqual(state["samples"][0]["courant"], {"timestep_s": 5e-3, "rms": 2.79, "max": 12.9})

    def test_summary_keeps_completion_convergence_and_end_time_separate(self) -> None:
        state = parse_cfx_log(LOG_HEADER + STEP + STEP2 + TAIL)
        summary = summarize(state)
        self.assertEqual(summary["time_steps"], 2)
        self.assertEqual(summary["last_time_step"], 2)
        self.assertEqual(summary["last_simulation_time_s"], 1e-2)
        self.assertTrue(summary["finished_marker"])
        self.assertIs(summary["converged"], True)
        self.assertEqual(summary["convergence_time_s"], 5e-3)
        self.assertEqual(summary["expected_end_time_s"], 5.0)
        self.assertIs(summary["end_time_reached"], False)
        self.assertIsNone(summary["monitor_values"])

    def test_expected_end_time_overrides_log_total(self) -> None:
        state = parse_cfx_log(STEP + STEP2)
        summary = summarize(state, expected_end_time=1e-2)
        self.assertIs(summary["end_time_reached"], True)
        self.assertIsNone(summary["residual_target"])
        self.assertIsNone(summary["converged"])

    def test_restart_rewind_is_counted(self) -> None:
        restart = " TIME STEP =     1 SIMULATION TIME = 5.0000E-03 CPU SECONDS = 9.0E+02\n"
        state = parse_cfx_log(STEP2 + restart)
        self.assertEqual(state["restarts"], 1)
        self.assertEqual(len(state["samples"]), 2)

    def test_non_finite_or_malformed_rows_are_not_invented(self) -> None:
        broken = (
            " TIME STEP =     1 SIMULATION TIME = 5.0000E-03 CPU SECONDS = 1.0E+02\n"
            " | U-Mom                | 0.10 | nan | 1.0E-04 |       1.0E-02  OK|\n"
            " | P-Mass               | 0.20 | 1.0E-03 | 2.0E-02 |\n"
        )
        state = parse_cfx_log(broken)
        self.assertEqual(state["samples"][0]["equations"], {})
        summary = summarize(state)
        self.assertEqual(summary["equations"], {})
        self.assertIsNone(summary["converged"])

    def test_partial_last_line_is_preserved_for_the_next_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "solver.out"
            log.write_bytes((LOG_HEADER + STEP).encode("utf-8"))
            first = read_cfx_log(log)
            self.assertTrue(first["ok"])
            self.assertEqual(first["state"]["samples"][-1]["time_step"], 1)
            next_step = STEP2.encode("utf-8")
            with log.open("ab") as stream:
                stream.write(next_step[:25])  # deliberately incomplete line
            second = read_cfx_log(log, offset=first["offset"], state=first["state"])
            self.assertTrue(second["ok"])
            self.assertEqual(second["state"]["samples"][-1]["time_step"], 1)
            self.assertEqual(second["state"]["samples"][-1]["equations"]["U-Mom"]["rms"], 4.7e-6)
            with log.open("ab") as stream:
                stream.write(next_step[25:])
            third = read_cfx_log(log, offset=second["offset"], state=second["state"])
            self.assertEqual(third["state"]["samples"][-1]["time_step"], 2)
            self.assertEqual(third["state"]["samples"][-1]["simulation_time_s"], 1e-2)

    def test_missing_log_reports_unknown_instead_of_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = read_cfx_log(Path(temporary) / "absent.out")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "missing")
        summary = summarize(empty_state())
        self.assertIsNone(summary["last_time_step"])
        self.assertIsNone(summary["converged"])


if __name__ == "__main__":
    unittest.main()
