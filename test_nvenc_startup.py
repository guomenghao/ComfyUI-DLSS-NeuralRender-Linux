"""Persistent startup/configuration tests with fake runtime files; no GPU use."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install_nvenc_startup as installer
from test_control_scripts import controller


def runtime_fixture(root):
    root = Path(root).resolve()
    runtime = root / "nvenc-runtime"
    runtime.mkdir()
    fake_ffmpeg, fake_ffprobe = root / "native-ffmpeg", root / "native-ffprobe"
    for file in (fake_ffmpeg, fake_ffprobe):
        file.write_text("#!/bin/sh\nexit 0\n")
        file.chmod(0o755)
    (runtime / "nvenc_fix.c").write_bytes((installer.ROOT / "nvenc_fix.c").read_bytes())
    # Only deployment-format validation is tested with this synthetic ELF header.
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62).to_bytes(2, "little")
    (runtime / "libnvenc_fix.so").write_bytes(header)
    (runtime / "ffmpeg").write_text(installer.wrapper_text(runtime / "libnvenc_fix.so", fake_ffmpeg))
    (runtime / "ffmpeg").chmod(0o755)
    report_dir = runtime / "gpu-test-fixture"
    report_dir.mkdir()
    report = {"status": "PASS", "source_sha256": installer.sha((runtime / "nvenc_fix.c").read_bytes()),
              "wrapper": str(runtime / "ffmpeg"), "tests": []}
    for size, width, height in (("256x256", 256, 256), ("1296x720", 1296, 720)):
        report["tests"].append({"size": size, "exit_code": 0,
             "decoded": {"codec_name": "h264", "width": width,
                         "height": height, "nb_read_frames": "12"}})
    report_path = report_dir / "results.json"
    report_path.write_text(json.dumps(report))
    return runtime, fake_ffmpeg, fake_ffprobe, report_path


class StartupInstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="nvenc-startup-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.comfy = self.root / "ComfyUI with spaces"
        self.comfy.mkdir()
        (self.comfy / "main.py").touch()
        boot_dir = self.root / "start.d"
        boot_dir.mkdir()
        self.boot = boot_dir / "comfyui.sh"
        self.boot.write_text("#!/bin/bash\n# original platform startup\n")
        self.runtime, self.ffmpeg, self.ffprobe, self.report = runtime_fixture(self.root)
        for name, value in (("FFMPEG", self.ffmpeg), ("FFPROBE", self.ffprobe)):
            patched = patch.object(installer, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_deployment_backups_and_rerun_is_idempotent(self):
        original = self.boot.read_bytes()
        backup = installer.install(self.comfy, self.boot, self.runtime)
        self.assertEqual((backup / "boot-comfyui.sh").read_bytes(), original)
        self.assertIn(str(self.comfy / "start.sh"), self.boot.read_text())
        self.assertEqual((self.comfy / "start.sh").read_bytes(), (installer.ROOT / "start.sh").read_bytes())
        saved = json.loads((self.comfy / "comfyui-runtime.json").read_text())
        self.assertEqual(saved["environment"]["DLSS_FFMPEG_PATH"], str(self.runtime / "ffmpeg"))
        self.assertIsNone(installer.install(self.comfy, self.boot, self.runtime))
        self.assertEqual(len(list((self.comfy / "node_backups").iterdir())), 1)

    def test_failed_or_wrong_size_report_cannot_change_boot(self):
        original = self.boot.read_bytes()
        saved = json.loads(self.report.read_text())
        saved["tests"][1]["decoded"]["nb_read_frames"] = "0"
        self.report.write_text(json.dumps(saved))
        with self.assertRaises(RuntimeError):
            installer.install(self.comfy, self.boot, self.runtime)
        self.assertEqual(self.boot.read_bytes(), original)
        self.assertFalse((self.comfy / "start.sh").exists())

    def test_symlink_destination_is_rejected_before_any_change(self):
        sentinel = self.root / "unrelated"
        sentinel.write_text("keep")
        (self.comfy / "start.sh").symlink_to(sentinel)
        original = self.boot.read_bytes()
        with self.assertRaises(RuntimeError):
            installer.install(self.comfy, self.boot, self.runtime)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual(self.boot.read_bytes(), original)

    def test_write_failure_restores_preexisting_files(self):
        old_start = b"#!/bin/bash\n# old manual startup\n"
        (self.comfy / "start.sh").write_bytes(old_start)
        original = self.boot.read_bytes()
        real_write = installer.atomic_write
        calls = 0
        def failing_write(target, data, mode):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("synthetic storage failure")
            real_write(target, data, mode)
        with patch.object(installer, "atomic_write", side_effect=failing_write):
            with self.assertRaises(OSError):
                installer.install(self.comfy, self.boot, self.runtime)
        self.assertEqual((self.comfy / "start.sh").read_bytes(), old_start)
        self.assertEqual(self.boot.read_bytes(), original)
        self.assertFalse((self.comfy / "stop.sh").exists())
        self.assertFalse((self.comfy / "comfyui-runtime.json").exists())

    def test_check_makes_no_changes_and_saved_env_survives_new_shell(self):
        installer.install(self.comfy, self.boot, self.runtime, check=True)
        self.assertFalse((self.comfy / "comfyui-runtime.json").exists())
        installer.install(self.comfy, self.boot, self.runtime)
        with patch.dict(os.environ, {}, clear=True):
            env = controller.start_environment(self.comfy)
            self.assertEqual(env["DLSS_FFMPEG_PATH"], str(self.runtime / "ffmpeg"))
            self.assertEqual(env["DLSS_FFPROBE_PATH"], str(self.ffprobe))
            self.assertNotIn("LD_PRELOAD", env)
            self.assertNotIn("DLSS_FFMPEG_PATH", os.environ)

    def test_missing_library_blocks_start_but_cpu_override_is_supported(self):
        installer.install(self.comfy, self.boot, self.runtime)
        (self.runtime / "libnvenc_fix.so").unlink()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                controller.start_environment(self.comfy)
        with patch.dict(os.environ, {"DLSS_FFMPEG_PATH": str(self.ffmpeg)}, clear=True):
            self.assertEqual(controller.start_environment(self.comfy)["DLSS_FFMPEG_PATH"], str(self.ffmpeg))


if __name__ == "__main__":
    unittest.main()
