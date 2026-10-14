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


if __name__ == "__main__": unittest.main()
