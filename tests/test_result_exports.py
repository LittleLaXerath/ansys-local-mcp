from __future__ import annotations

import json
import os
import struct
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from tools.run_result_exports import digest_file, png_metadata, probe_media, validate


def png_chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


class ResultExportVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.png = self.root / "image.png"
        raw = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        raw += png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff")) + png_chunk(b"IEND", b"")
        self.png.write_bytes(raw)

    def test_png_dimensions_and_crc_are_independently_checked(self):
        result = png_metadata(self.png)
        self.assertEqual((result["width"], result["height"]), (1, 1))
        original = self.png.read_bytes()
        self.png.write_bytes(original[:-1])
        with self.assertRaises(ValueError):
            png_metadata(self.png)
        damaged = bytearray(original)
        damaged[30] ^= 1
        self.png.write_bytes(damaged)
        with self.assertRaises(ValueError):
            png_metadata(self.png)

    def test_animation_must_decode_all_frames(self):
        animation = self.root / "animation.gif"
        animation.write_bytes(b"fixture")
        metadata = {"streams": [{"codec_name": "gif", "width": 800, "height": 600,
                                 "nb_read_frames": "24", "duration": "2"}], "format": {"format_name": "gif", "duration": "2"}}
        success = types.SimpleNamespace(stdout=json.dumps(metadata), returncode=0, stderr="")
        decoded = types.SimpleNamespace(stdout="", returncode=0, stderr="")
        with patch("tools.run_result_exports.shutil.which", side_effect=lambda name: name), \
             patch("tools.run_result_exports.subprocess.run", side_effect=[success, decoded]):
            self.assertEqual(probe_media(animation)["frames"], 24)
        corrupted = types.SimpleNamespace(stdout="", returncode=1, stderr="decoder failure")
        with patch("tools.run_result_exports.shutil.which", side_effect=lambda name: name), \
             patch("tools.run_result_exports.subprocess.run", side_effect=[success, corrupted]):
            with self.assertRaises(ValueError):
                probe_media(animation)

    def test_csv_conversion_requires_explicit_table_unit_and_source_preservation(self):
        table = self.root / "deformation.txt"
        table.write_text("Node Number\tTotal Deformation (mm)\n1\t0\n2\t0.002\n", encoding="utf-8")
        animation = self.root / "animation.gif"
        animation.write_bytes(b"fixture")
        project = self.root / "source.wbpj"
        project.write_text("source", encoding="utf-8")
        report = {"png_path": str(self.png), "animation_path": str(animation), "animation_format": "GIF",
                  "table_path": str(table), "table_length_scale_m": 0.001, "maximum_deformation_m": 2e-6,
                  "solution_status_before": "Done", "solve_called": False, "status": "exported", "active_phase": "complete"}
        parameters = {"source_project": str(project), "source_sha256": digest_file(project), "width": 1, "height": 1, "frames": 24, "duration_s": 2}
        media = {"codec": "gif", "container": "gif", "width": 1, "height": 1, "frames": 24, "duration_s": 2, "decoded_all_frames": True}
        with patch.dict(os.environ, {"ANSYS_MCP_ALLOWED_ROOTS": str(self.root)}), \
             patch("tools.run_result_exports.probe_media", return_value=media):
            self.assertTrue(validate(self.root, report, parameters)["ok"])
            self.assertIn("2e-06", (self.root / "deformation.csv").read_text(encoding="utf-8"))
            animation.unlink()
            partial = validate(self.root, report, parameters)
            self.assertFalse(partial["ok"])
            self.assertTrue(partial["checks"]["png_dimensions"])
            self.assertTrue(partial["checks"]["csv_nodes"])
            project.write_text("changed", encoding="utf-8")
            self.assertFalse(validate(self.root, report, parameters)["ok"])


if __name__ == "__main__":
    unittest.main()
