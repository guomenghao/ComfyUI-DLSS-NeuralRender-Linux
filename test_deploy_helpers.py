import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("v2d_configure_test", ROOT / "configure_local.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def pe(machine):
    header = bytearray(64)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 60, 64)
    return bytes(header) + b"PE\0\0" + struct.pack("<H", machine)


class DeployHelpersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_pe_machine_rejects_32bit_and_lfs_pointer(self):
        file = self.root / "bridge.dll"
        file.write_bytes(pe(0x8664))
        self.assertTrue(helper.pe64(file))
        file.write_bytes(pe(0x14c))
        self.assertFalse(helper.pe64(file))
        file.write_text("version https://git-lfs.github.com/spec/v1\n")
        self.assertFalse(helper.pe64(file))

    def test_atomic_copy_does_not_write_through_destination_symlink(self):
        src = self.root / "source.dll"
        other = self.root / "unrelated-driver.dll"
        dest = self.root / "system32/dll"
        src.write_bytes(b"native DLL")
        other.write_bytes(b"do not change")
        dest.parent.mkdir()
        dest.symlink_to(other)
        helper.atomic_copy(src, dest)
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_bytes(), src.read_bytes())
        self.assertEqual(other.read_bytes(), b"do not change")

    def test_unmanaged_or_symlink_prefix_refused(self):
        prefix = self.root / "prefix"
        helper.validate_prefix(prefix)
        prefix.mkdir()
        with self.assertRaisesRegex(RuntimeError, "unmanaged"):
            helper.validate_prefix(prefix)
        (prefix / ".video2dlssnr-owner").write_text(helper.MARKER)
        helper.validate_prefix(prefix)
        linked = self.root / "linked"
        linked.symlink_to(prefix, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            helper.validate_prefix(linked)


if __name__ == "__main__":
    unittest.main()
