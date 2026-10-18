"""Provider-boundary regression tests; no Ansys software is launched."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ansys_mcp.mechanical import MechanicalError, WorkbenchBackend


class ScriptWorkbench:
    def __init__(self):
        self.namespace = {}

    def run_script_string(self, source, **kwargs):
        exec(source, self.namespace)
        return json.loads(self.namespace["wb_script_result"])


class WorkbenchBoundaryTests(unittest.TestCase):
    def test_void_result_distinguished_from_product_script_exception(self):
        backend = WorkbenchBackend()
        backend.workbench = ScriptWorkbench()
        self.assertIsNone(backend._wb_run("pass"))
        with self.assertRaises(MechanicalError) as caught:
            backend._wb_run("raise RuntimeError('synthetic template not found')")
        self.assertEqual(caught.exception.code, "MECHANICAL_WORKBENCH_API_ERROR")
        self.assertIn("synthetic template", str(caught.exception))

    def test_sdk_none_is_unknown_instead_of_a_valid_void_result(self):
        backend = WorkbenchBackend()
        backend.workbench = SimpleNamespace(run_script_string=Mock(return_value=None))
        with self.assertRaises(MechanicalError) as caught:
            backend._wb_run("pass")
        self.assertEqual(caught.exception.code, "MECHANICAL_PROTOCOL_ERROR")

    def test_invalid_port_never_connects_to_implicit_default_port(self):
        for port in (None, 0, "10000", False, -1, 65536):
            with self.subTest(port=port):
                backend = WorkbenchBackend()
                backend.system_name = "SYS"
                backend._wb_run = Mock(return_value=port)
                backend._mechanical_module = SimpleNamespace(connect_to_mechanical=Mock())
                with self.assertRaises(MechanicalError):
                    backend._connect_model()
                backend._mechanical_module.connect_to_mechanical.assert_not_called()

    def test_workbench_system_name_is_a_literal_not_embedded_script(self):
        backend = WorkbenchBackend()
        backend.system_name = 'quoted"system'
        backend._wb_run = Mock(return_value=12345)
        backend._mechanical_module = SimpleNamespace(connect_to_mechanical=Mock(return_value=object()))
        backend._track_mechanical = Mock()
        backend._connect_model()
        self.assertIn('SystemName=u"quoted\\\"system"', backend._wb_run.call_args.args[0])
        backend._mechanical_module.connect_to_mechanical.assert_called_once_with(
            ip="127.0.0.1", port=12345, cleanup_on_exit=False, clear_on_connect=False,
        )


if __name__ == "__main__":
    unittest.main()
