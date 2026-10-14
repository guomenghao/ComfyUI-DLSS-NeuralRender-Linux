import tempfile
import unittest
import zipfile
import hashlib
from pathlib import Path
from unittest.mock import patch
import fetch_runtime as runtime


class FetchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "source.zip"
        self.node = self.root / "node"
        self.node.mkdir()
        (self.node / "nodes.py").write_text("retain modified Python")
        self.files = {"worker.exe": hashlib.sha256(b"MZworker").hexdigest()}

    def test_windows_paths_extract_only_runtime_and_retain_code(self):
        with zipfile.ZipFile(self.archive,"w") as z:
            z.writestr("video2dlssnr\\bin\\worker.exe", b"MZworker")
            z.writestr("video2dlssnr\\nodes.py", "upstream Python")
        with patch.object(runtime,"verify_archive"), patch.object(runtime,"FILES",self.files):
            runtime.install_archive(self.archive,self.node)
        self.assertEqual((self.node/"nodes.py").read_text(),"retain modified Python")
        self.assertEqual((self.node/"bin/worker.exe").read_bytes(),b"MZworker")

    def test_mismatch_or_traversal_never_modifies_destination(self):
        for path,content in [("video2dlssnr/bin/worker.exe",b"wrong"),("../../outside",b"bad")]:
            with self.subTest(path=path):
                with zipfile.ZipFile(self.archive,"w") as z:
                    z.writestr(path,content)
                with patch.object(runtime,"verify_archive"), patch.object(runtime,"FILES",self.files):
                    with self.assertRaises(RuntimeError): runtime.install_archive(self.archive,self.node)
                self.assertFalse((self.node/"bin").exists())

    def test_existing_different_runtime_is_preserved(self):
        (self.node/"bin").mkdir();(self.node/"bin/worker.exe").write_bytes(b"custom")
        with zipfile.ZipFile(self.archive,"w") as z:
            z.writestr("video2dlssnr/bin/worker.exe",b"MZworker")
        with patch.object(runtime,"verify_archive"), patch.object(runtime,"FILES",self.files):
            with self.assertRaisesRegex(RuntimeError,"Refusing"):
                runtime.install_archive(self.archive,self.node)
        self.assertEqual((self.node/"bin/worker.exe").read_bytes(),b"custom")

    def test_backup_reuse_verifies_files_and_preserves_python(self):
        donor = self.root / "backup" / "bin"
        donor.mkdir(parents=True)
        (donor / "worker.exe").write_bytes(b"MZworker")
        with patch.object(runtime, "FILES", self.files):
            self.assertTrue(runtime.reuse_binaries(self.node, [donor]))
            self.assertEqual(runtime.missing_binaries(self.node), [])
        self.assertEqual((self.node / "nodes.py").read_text(), "retain modified Python")
        self.assertEqual((donor / "worker.exe").read_bytes(), b"MZworker")

    def test_incomplete_or_bad_backup_leaves_node_untouched(self):
        donor = self.root / "backup"
        donor.mkdir()
        (donor / "worker.exe").write_bytes(b"wrong")
        with patch.object(runtime, "FILES", self.files):
            self.assertFalse(runtime.reuse_binaries(self.node, [donor]))
        self.assertFalse((self.node / "bin").exists())

    def test_reuse_rejects_existing_modified_binary(self):
        (self.node / "bin").mkdir()
        target = self.node / "bin" / "worker.exe"
        target.write_bytes(b"custom")
        with patch.object(runtime, "FILES", self.files):
            with self.assertRaisesRegex(RuntimeError, "Refusing"):
                runtime.reuse_binaries(self.node, [])
        self.assertEqual(target.read_bytes(), b"custom")

    def test_old_zip_partial_is_retained_for_resume(self):
        legacy = self.archive.with_suffix(".part")
        legacy.write_bytes(b"previous progress")
        partial = Path(str(self.archive) + ".part")
        with patch.object(runtime, "fetch") as downloader, patch.object(runtime, "verify_archive"):
            runtime.download_archive(self.archive, "https://example.com/pinned.zip")
        self.assertFalse(legacy.exists())
        self.assertEqual(partial.read_bytes(), b"previous progress")
        downloader.assert_called_once_with("https://example.com/pinned.zip", self.archive, runtime.ARCHIVE_SHA256)

    def test_new_partial_does_not_overwrite_old_partial(self):
        legacy = self.archive.with_suffix(".part")
        partial = Path(str(self.archive) + ".part")
        legacy.write_bytes(b"old")
        partial.write_bytes(b"new")
        with patch.object(runtime, "fetch"), patch.object(runtime, "verify_archive"):
            runtime.download_archive(self.archive, runtime.URL)
        self.assertEqual(legacy.read_bytes(), b"old")
        self.assertEqual(partial.read_bytes(), b"new")


if __name__ == "__main__": unittest.main()
