"""Private same-user execution worker. The wire protocol uses JSON, not pickle."""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any

# ShellExecuteEx starts a script directly; make the installed package importable
# without depending on the caller's working directory or a user profile path.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ansys_mcp.jobs import _read_json, _write_json
from ansys_mcp.process_supervisor import (
    CancellationToken, WorkflowCancelled, cancellation_context, prime_python_com_cache,
    process_identity, process_identity_matches, terminate_owned_process,
)


def main() -> None:
    bootstrap_path = Path(sys.argv[1]).resolve(strict=True)
    directory = bootstrap_path.parent
    # Pin pywin32's generated-type cache to this session's private directory
    # before the handler (and any product import it performs) is loaded.
    prime_python_com_cache(directory / "comcache")
    bootstrap = _read_json(bootstrap_path)
    sys.stdout = (directory / "worker.stdout.log").open("a", encoding="utf-8", buffering=1)
    sys.stderr = (directory / "worker.stderr.log").open("a", encoding="utf-8", buffering=1)
    os.environ["ANSYS_MCP_ALLOWED_ROOTS"] = ";".join(bootstrap["allowed_roots"])
    try:
        module_name = bootstrap["handler_module"]
        if not re.fullmatch(r"ansys_mcp(?:\.[a-zA-Z_][a-zA-Z_0-9]*)+", module_name):
            raise ValueError("Invalid execution handler module.")
        if bootstrap.get("elevated"):
            import ctypes
            if os.name != "nt" or not ctypes.windll.shell32.IsUserAnAdmin():
                raise PermissionError("Execution worker does not have an elevated Windows token.")
        handler = getattr(importlib.import_module(module_name), bootstrap["handler_function"])
        listener = Listener(("127.0.0.1", 0), authkey=bytes.fromhex(bootstrap["authkey"]))
        _write_json(directory / "ready.json", {"worker": process_identity(os.getpid()), "address": list(listener.address), "elevated": bootstrap.get("elevated", False)})
    except BaseException as exc:
        _write_json(directory / "startup-error.json", {"message": str(exc)})
        return
    records: dict[str, dict[str, Any]] = {}
    tokens: dict[str, CancellationToken] = {}
    lock = threading.RLock()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ansys-mcp-privileged")

    def execute(request_id: str, operation: str, payload: dict[str, Any]) -> None:
        with lock:
            records[request_id] = {"status": "running", "request_id": request_id}
        try:
            token = tokens[request_id]
            token.raise_if_cancelled()
            with cancellation_context(token):
                result = handler(operation, payload)
            # Validate serializability before declaring the operation complete.
            json.dumps(result)
            record = {"status": "cancelled" if token.cancelled else "complete", "request_id": request_id, "result": result}
        except WorkflowCancelled as exc:
            record = {"status": "cancelled", "request_id": request_id, "result": {
                "ok": False, "status": "cancelled", "diagnostics": [{"code": "WORKFLOW_CANCELLED", "message": str(exc)}],
            }}
        except BaseException as exc:
            record = {"status": "failed", "request_id": request_id, "error": str(exc)}
        with lock:
            records[request_id] = dict(records[request_id], **record)
            _write_json(directory / (request_id + ".json"), records[request_id])

    def cancel_after_grace(request_id: str, grace_seconds: float) -> None:
        # This runs inside the administrator process, so no privilege boundary
        # is crossed by TerminateProcess. Every target has a creation identity.
        import time
        time.sleep(grace_seconds)
        token = tokens[request_id]
        forced = []
        for identity in token.owned_processes():
            if terminate_owned_process(identity):
                forced.append(identity)
        with lock:
            records[request_id]["cancel_requested"] = True
            records[request_id]["forced_process_termination"] = forced
            _write_json(directory / (request_id + ".json"), records[request_id])

    try:
        while True:
            try:
                connection = listener.accept()
            except AuthenticationError:
                continue
            stop = False
            try:
                if not connection.poll(5):
                    continue
                request = json.loads(connection.recv_bytes(16 * 1024 * 1024))
                command = request.get("command")
                request_id = request.get("request_id", "")
                with lock:
                    if command == "execute":
                        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
                            response = {"ok": False, "error": "Invalid request identity."}
                        elif request_id in records:
                            response = {"ok": False, "error": "Request already exists; query its result instead of replaying it."}
                        else:
                            tokens[request_id] = CancellationToken(directory / (request_id + ".cancel.json"))
                            records[request_id] = {"status": "queued", "request_id": request_id}
                            executor.submit(execute, request_id, request["operation"], request.get("payload", {}))
                            response = {"ok": True, "request_id": request_id}
                    elif command == "query":
                        response = dict(records.get(request_id, {"status": "unknown", "request_id": request_id}))
                        if request_id in tokens:
                            response["owned_processes"] = [identity for identity in tokens[request_id].owned_processes() if process_identity_matches(identity)]
                            response["cancel_requested"] = tokens[request_id].cancelled
                    elif command == "cancel":
                        record = records.get(request_id)
                        unknown_result = isinstance((record or {}).get("result"), dict) and record["result"].get("status") == "unknown"
                        if record is None:
                            response = {"ok": False, "error": "Unknown request identity; nothing was cancelled."}
                        elif record["status"] in {"complete", "failed", "cancelled"} and not unknown_result:
                            response = {"ok": False, "status": record["status"], "error": "Request is already terminal; nothing was cancelled."}
                        elif record.get("cancel_requested"):
                            response = {"ok": True, "status": "cancel_requested", "request_id": request_id}
                        else:
                            tokens[request_id].cancel()
                            _write_json(directory / (request_id + ".cancel.json"), {"request_id": request_id})
                            record["cancel_requested"] = True
                            grace = min(60.0, max(0.1, float(request.get("grace_seconds", 5))))
                            threading.Thread(target=cancel_after_grace, args=(request_id, grace), daemon=True).start()
                            response = {"ok": True, "status": "cancel_requested", "request_id": request_id}
                    elif command in {"summary", "close"}:
                        pending = [value for value in records.values() if value["status"] in {"queued", "running"}]
                        owned = {str(identity): identity for token in tokens.values() for identity in token.owned_processes() if process_identity_matches(identity)}
                        response = {"ok": True, "pending": pending, "worker": process_identity(os.getpid()), "owned_processes": list(owned.values())}
                        if command == "close":
                            response["ok"] = not pending and not owned
                            stop = response["ok"]
                            if not stop:
                                response["error"] = "Requests or owned product processes remain; cancel requests or close product sessions before closing this worker."
                    else:
                        response = {"ok": False, "error": "Unknown session command."}
                connection.send_bytes(json.dumps(response).encode("utf-8"))
            except (EOFError, OSError, ValueError, KeyError, TypeError):
                pass
            finally:
                connection.close()
            if stop:
                break
    finally:
        listener.close()
        executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
