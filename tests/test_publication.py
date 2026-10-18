from __future__ import annotations

import json
import gzip
import io
import tarfile
import tempfile
import shutil
import subprocess
import unittest
import zipfile
from pathlib import Path

from ansys_mcp.publication import scan_archive, scan_bytes, scan_commit, scan_tree


class PublicationTests(unittest.TestCase):
    def test_machine_fingerprint_is_detected_without_disclosing_value(self):
        value = "synthetic-host-42"
        findings = scan_bytes("summary.json", json.dumps({"host": value}).encode(), [value])
        self.assertTrue(findings)
        self.assertNotIn(value, json.dumps(findings))

    def test_personal_profile_and_escaped_path(self):
        personal = "C:" + "/Users/" + "synthetic-person/project"
        findings = scan_bytes("settings.json", json.dumps({"path": personal.replace("/", "\\")}).encode())
        self.assertIn("personal_profile_path", {item["category"] for item in findings})

    def test_generic_paths_and_loopback_are_not_machine_identity(self):
        example = {"workdir": "C:" + "/Users/" + "YOUR-NAME/work", "endpoint": "https://localhost:0", "root": "C:/Program Files/ANSYS Inc"}
        self.assertEqual(scan_bytes("example.json", json.dumps(example).encode()), [])

    def test_private_service_and_credentials_report_only_categories(self):
        endpoint = "http://" + "192.168." + "2.9:1234"
        secret = "ghp_" + "a" * 36
        findings = scan_bytes("config.txt", (endpoint + "\n" + secret).encode())
        self.assertEqual({item["category"] for item in findings}, {"private_service_url", "credential_pattern"})
        self.assertNotIn(secret, json.dumps(findings))

    def test_json_worker_auth_key_is_not_publishable(self):
        key = "b" * 64
        findings = scan_bytes("bootstrap.json", json.dumps({"authkey": key}).encode())
        self.assertIn("credential_literal", {item["category"] for item in findings})
        self.assertNotIn(key, json.dumps(findings))

    def test_distribution_contents_are_checked_not_only_git_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "synthetic.whl"
            with zipfile.ZipFile(archive, "w") as stream:
                stream.writestr("adapter/.env", "dummy")
                stream.writestr("adapter/model" + ".mechdb", b"binary metadata")
            result = scan_archive(archive)
            self.assertFalse(result["ok"])
            self.assertEqual(result["files_checked"], 2)
            self.assertEqual({item["category"] for item in result["findings"]}, {"private_artifact"})

    def test_scanner_source_is_not_misclassified_as_a_private_key(self):
        source = Path(__file__).parents[1] / "ansys_mcp" / "publication.py"
        self.assertEqual(scan_bytes("ansys_mcp/publication.py", source.read_bytes()), [])

    def test_unknown_binary_is_not_a_public_source_attachment(self):
        self.assertIn("binary_artifact", {item["category"] for item in scan_bytes("unknown.input", b"\x00\xff\x80")})

    def test_native_backups_and_compound_result_suffixes_are_private(self):
        for suffix in (".mechdat", ".mechdb.bak", ".mechdat.bak", ".wbpj.bak", ".cas.h5", ".dat.h5"):
            with self.subTest(suffix=suffix):
                findings = scan_bytes("synthetic" + suffix, b"synthetic text metadata")
                self.assertIn("private_artifact", {item["category"] for item in findings})

    def test_gzip_and_global_tar_metadata_are_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "example.tar.gz"
            content = io.BytesIO()
            with tarfile.open(fileobj=content, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": "synthetic-host-42"}) as archive:
                member = tarfile.TarInfo("public.txt")
                member.size = 6
                archive.addfile(member, io.BytesIO(b"public"))
            with target.open("wb") as output:
                with gzip.GzipFile(filename="synthetic-host-42.tar", fileobj=output, mode="wb") as compressed:
                    compressed.write(content.getvalue())
            result = scan_archive(target, ["synthetic-host-42"])
            self.assertFalse(result["ok"])
            self.assertNotIn("synthetic-host-42", json.dumps(result))

    def test_zip_directory_metadata_and_links_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "example.whl"
            with zipfile.ZipFile(target, "w") as archive:
                member = zipfile.ZipInfo("public/")
                member.comment = b"synthetic-host-42"
                archive.writestr(member, b"")
                link = zipfile.ZipInfo("public/link")
                link.create_system = 3
                link.external_attr = 0o120777 << 16
                archive.writestr(link, "target")
            result = scan_archive(target, ["synthetic-host-42"])
            self.assertEqual(result["files_checked"], 1)
            categories = {item["category"] for item in result["findings"]}
            self.assertTrue({"machine_fingerprint", "archive_link"} <= categories)
            self.assertNotIn("synthetic-host-42", json.dumps(result))

    @unittest.skipUnless(shutil.which("git"), "Git required for index snapshot acceptance")
    def test_staged_blob_is_checked_even_when_worktree_file_is_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args], stderr=subprocess.DEVNULL)
            git("init")
            target = root / "public.txt"
            target.write_text("synthetic-host-42", encoding="utf-8")
            git("add", "public.txt")
            target.unlink()
            result = scan_tree(root, ["synthetic-host-42"], staged=True)
            self.assertFalse(result["ok"])
            self.assertIn("machine_fingerprint", {item["category"] for item in result["findings"]})

    @unittest.skipUnless(shutil.which("git"), "Git required for commit metadata acceptance")
    def test_commit_identity_and_message_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(["git", "-c", "safe.directory=" + str(root), "-c", "commit.gpgsign=false", "-c", "user.name=public-fixture", "-c", "user.email=fixture@users.noreply.github.com", "-C", str(root), *args], stderr=subprocess.DEVNULL)
            git("init")
            (root / "example.txt").write_text("public synthetic input", encoding="utf-8")
            git("add", "example.txt")
            git("commit", "-m", "Add synthetic input")
            self.assertTrue(scan_commit(root, "HEAD", "fixture@users.noreply.github.com", expected_name="public-fixture")["ok"])
            self.assertFalse(scan_commit(root, "HEAD", "fixture@users.noreply.github.com", expected_name="different-fixture")["ok"])
            git("commit", "--allow-empty", "-m", "Mention synthetic-host-42")
            result = scan_commit(root, "HEAD", "fixture@users.noreply.github.com", ["synthetic-host-42"], "public-fixture")
            self.assertFalse(result["ok"])
            self.assertNotIn("synthetic-host-42", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
