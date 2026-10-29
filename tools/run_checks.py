"""Record the non-product test suite with exact per-test outcomes."""
from __future__ import annotations

import argparse
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ansys_mcp.config import require_allowed_path
from ansys_mcp.jobs import _write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Private JSON report; full log is written beside it")
    args = parser.parse_args()
    output = require_allowed_path(args.output, expect_file=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["ANSYS_MCP_PRODUCT_TESTS"] = "0"
    os.environ["ANSYS_MCP_RUN_ELEVATION_TESTS"] = "0"
    project = Path(__file__).resolve().parents[1]
    os.chdir(project)
    log = output.with_suffix(".log")
    with log.open("w", encoding="utf-8") as stream:
        suite = unittest.defaultTestLoader.discover(str(project / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    failed_ids = {test.id() for test, _ in [*result.failures, *result.errors]}
    summary = {
        "observed_at": datetime.now(timezone.utc).isoformat(), "ok": result.wasSuccessful(),
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "affected_tests": sorted(failed_ids), "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "passed_tests": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
        "log_path": str(log), "product_tests_enabled": False, "uac_tests_enabled": False,
    }
    _write_json(output, summary)
    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    main()
