"""Real subprocess/pipe tests with a fake Wine worker; never claims GPU validation."""
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import numpy.testing

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("v2d_test_package", ROOT / "__init__.py",
                                           submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
nodes = sys.modules[spec.name + ".nodes"]
runtime = sys.modules[spec.name + ".wine_runtime"]

FAKE_WINE = r'''
import os, pathlib, sys, time
args = sys.argv[2:]
def value(key, default=None):
    return args[args.index(key)+1] if key in args else default
def unix_path(raw):
    drive = pathlib.Path(os.environ['WINEPREFIX']) / 'dosdevices' / raw[:2].lower()
    return drive.resolve() / raw[3:].replace('\\', '/')
mode = os.environ.get('TEST_WORKER_MODE', 'normal')
if mode == 'hang':
    time.sleep(60)
if mode == 'descendant':
    import subprocess
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    sys.exit(0)
if mode == 'fail':
    sys.stderr.write('NGX initialization failed in fake worker\n')
    sys.exit(73)
if mode == 'stdout-crash':
    sys.stdout.buffer.write(b'wine: Unhandled exception c0000005\n' + b'A' * 10200 + b'\ncrash report end\n')
    sys.stdout.buffer.flush()
    sys.exit(1)
if '--help' in args:
    print('video2dlssnr fake help: transport only')
    sys.exit(0)
if '--nr-run' in args:
    from PIL import Image
    target = unix_path(value('--out'))
    target.mkdir(parents=True, exist_ok=True)
    Image.open(unix_path(value('--in'))).save(target / 'input_nr.png')
    sys.exit(0)
if '--nr-video' in args:
    import numpy as np
    w, h = map(int, value('--nr-in').split('x'))
    dtype = np.dtype('<u2') if '--nr-transfer' in args else np.dtype('u1')
    raw = sys.stdin.buffer.read()
    frames = np.frombuffer(raw, dtype).reshape(-1, h, w, 4)
    ow, oh = int(value('--nr-width', w)), int(value('--nr-height', h))
    # Synthetic nearest-neighbor output verifies sizing/transport, not DLSS.
    frames = frames[:, np.arange(oh)*h//oh][:, :, np.arange(ow)*w//ow]
    data = frames.tobytes()
    if mode == 'short': data = data[:-7]
    if mode == 'extra': data += b'LOG POLLUTION'
    sys.stderr.write('fake Wine diagnostic (not an NR result)\n')
    sys.stderr.write('NRPROG %d 1.0\n' % len(frames))
    # Multiple writes exercise partial pipe delivery.
    for offset in range(0, len(data), 17):
        sys.stdout.buffer.write(data[offset:offset+17])
    sys.stdout.buffer.flush()
'''


class WineTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v2d 空格 " $ literal ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = self.root / "prefix"
        (self.prefix / "drive_c/windows/system32").mkdir(parents=True)
        (self.prefix / "dosdevices").mkdir()
        (self.prefix / "dosdevices/c:").symlink_to(self.prefix / "drive_c", target_is_directory=True)
        (self.prefix / "dosdevices/z:").symlink_to("/", target_is_directory=True)
        self.bin = self.root / "node/bin"
        self.bin.mkdir(parents=True)
        self.exe = self.bin / "video2dlssnr.exe"
        for name in ["video2dlssnr.exe", "nvngx_dlss.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll"]:
            (self.bin / name).write_bytes(b"MZfake PE header; never executed")
        self.wine = self.root / "fake wine"
        self.wine.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE_WINE))
        self.wine.chmod(0o755)
        self.config = self.root / "missing-config.json"
        self.env = {"VIDEO2DLSSNR_WINE": str(self.wine), "WINEPREFIX": str(self.prefix),
                    "VIDEO2DLSSNR_TIMEOUT": "15", "TEST_WORKER_MODE": "normal"}
        for context in [patch.object(runtime.sys, "platform", "linux"),
                        patch.object(runtime.platform, "machine", return_value="x86_64"),
                        patch.object(runtime, "CONFIG_PATH", self.config),
                        patch.dict(os.environ, self.env)]:
            context.start()
            self.addCleanup(context.stop)

    def frames(self, dtype=np.uint8):
        return np.arange(2*4*6*3, dtype=dtype).reshape(2, 4, 6, 3)

    def run_video(self, **kwargs):
        return nodes.run_video_np(self.frames(), 0, 1, [], True, "lk", False,
                                  str(self.exe), **kwargs)

    def test_windows_keeps_native_command_and_ignores_linux_config(self):
        self.config.write_text("invalid JSON")
        with patch.object(runtime.sys, "platform", "win32"):
            cmd, opts, _ = runtime.launch_spec(str(self.exe), ["--in", "C:\\image.png"])
        self.assertEqual(cmd, [str(self.exe.resolve()), "--in", "C:\\image.png"])
        self.assertNotIn("start_new_session", opts)

    def test_actual_drive_mapping_and_nonexistent_output(self):
        path = self.prefix / "drive_c/new output/中文.png"
        self.assertEqual(runtime.windows_path(str(path), self.prefix), "C:\\new output\\中文.png")
        self.assertTrue(runtime.windows_path(str(self.exe), self.prefix).startswith("Z:\\"))
        (self.prefix / "dosdevices/z:").unlink()
        with self.assertRaisesRegex(RuntimeError, "not accessible"):
            runtime.windows_path(str(self.exe), self.prefix)

    def test_worker_environment_is_local_and_saved_settings_win(self):
        self.config.write_text(json.dumps({"version": 1, "timeout_seconds": 22,
            "environment": {"WINEDEBUG": "-all", "DXVK_ENABLE_NVAPI": "1"}}))
        with patch.dict(os.environ, {"WINEDEBUG": "+all", "DXVK_ENABLE_NVAPI": "0"}):
            _, opts, timeout = runtime.launch_spec(str(self.exe), [])
            self.assertEqual(opts["env"]["WINEDEBUG"], "-all")
            self.assertEqual(opts["env"]["DXVK_ENABLE_NVAPI"], "1")
            self.assertEqual(os.environ["WINEDEBUG"], "+all")
            self.assertEqual(timeout, 22)

    def test_invalid_config_missing_prefix_wine_and_pointer_are_actionable(self):
        for contents in ["{", '{"version":2,"environment":{}}',
                         '{"version":1,"environment":{"UNKNOWN":"1"}}']:
            self.config.write_text(contents)
            with self.assertRaisesRegex(RuntimeError, "Invalid"):
                runtime.launch_spec(str(self.exe), [])
        self.config.unlink()
        with patch.dict(os.environ, {"WINEPREFIX": "relative"}):
            with self.assertRaisesRegex(RuntimeError, "absolute"):
                runtime.launch_spec(str(self.exe), [])
        with patch.dict(os.environ, {"VIDEO2DLSSNR_WINE": "/absent/wine --arg"}):
            with self.assertRaisesRegex(RuntimeError, "Wine not found"):
                runtime.launch_spec(str(self.exe), [])
        (self.bin / "nvngx_dlssnr.dll").write_text("version https://git-lfs.github.com/spec/v1\n")
        with self.assertRaisesRegex(RuntimeError, "Git LFS pointer"):
            runtime.launch_spec(str(self.exe), [])

    def test_image_paths_spaces_unicode_and_literal_shell_characters(self):
        src = self.frames()[0]
        original_mkdtemp = tempfile.mkdtemp
        with patch.object(nodes.tempfile, "mkdtemp", side_effect=lambda prefix:
                          original_mkdtemp(prefix=prefix, dir=self.root)):
            result = nodes.run_image_np(src, 0, 1, [], str(self.exe))
        np.testing.assert_array_equal(result, src)
        self.assertEqual(list(self.root.glob("v2d_*")), [])

    def test_sdr_binary_frames_and_stderr_progress(self):
        logs, progress = [], []
        result = self.run_video(log=logs, progress=progress.append)
        np.testing.assert_array_equal(result, self.frames())
        self.assertEqual(progress, [2])
        self.assertIn("fake Wine diagnostic", "\n".join(logs))

    def test_headless_wrapper_preserves_binary_pipe(self):
        wrapper = self.root / "xvfb-run"
        wrapper.write_text("#!" + sys.executable + "\nimport os, sys\nos.execv(sys.argv[3], sys.argv[3:])\n")
        wrapper.chmod(0o755)
        with patch.dict(os.environ, {"VIDEO2DLSSNR_XVFB": "1",
                                     "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", "")}):
            command, _, _ = runtime.launch_spec(str(self.exe), [])
            self.assertEqual(command[:2], [str(wrapper), "-a"])
            np.testing.assert_array_equal(self.run_video(), self.frames())

    def test_missing_headless_wrapper_fails_before_launch(self):
        original_which = runtime.shutil.which
        with patch.dict(os.environ, {"VIDEO2DLSSNR_XVFB": "1"}), patch.object(
                runtime.shutil, "which", side_effect=lambda name:
                None if name == "xvfb-run" else original_which(name)):
            with self.assertRaisesRegex(RuntimeError, "xvfb-run"):
                runtime.launch_spec(str(self.exe), [])

    def test_hdr_16bit_pipe_and_output_dimensions(self):
        src = self.frames(np.uint16) * 401
        result = nodes.run_video_np(src, 12, 2, [], True, "lk", False,
                                   str(self.exe), transfer="pq")
        self.assertEqual(result.dtype, np.uint16)
        self.assertEqual(result.shape, (2, 8, 12, 3))
        np.testing.assert_array_equal(result[:, ::2, ::2], src)

    def test_short_extra_and_nonzero_output_are_rejected(self):
        for mode, message in [("short", "got"), ("extra", "extra stdout"),
                              ("fail", "NGX initialization failed")]:
            with self.subTest(mode=mode), patch.dict(os.environ, {"TEST_WORKER_MODE": mode}):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_video()

    def test_stdout_crash_report_survives_empty_stderr(self):
        logs = []
        with patch.dict(os.environ, {"TEST_WORKER_MODE": "stdout-crash"}):
            with self.assertRaisesRegex(RuntimeError, "Unhandled exception c0000005") as failure:
                nodes.run_video_np(np.zeros((2, 64, 64, 3), np.uint8), 0, 1, [],
                                   True, "lk", False, str(self.exe), log=logs)
        self.assertIn("crash report end", str(failure.exception))
        self.assertIn("Unhandled exception c0000005", "\n".join(logs))

    def test_failed_stdout_preview_bounds_binary_and_terminal_controls(self):
        binary = nodes.failed_stdout_preview(b"\x00\xff\x01" * 20000)
        self.assertIn("first 64 bytes (hex)", binary)
        self.assertLess(len(binary), 400)
        text = nodes.failed_stdout_preview(b"HEADER\n\x1b[31m" + b"A" * 20000 + b"\nTAIL")
        self.assertIn("HEADER", text)
        self.assertIn("TAIL", text)
        self.assertIn("truncated", text)
        self.assertNotIn("\x1b", text)
        self.assertLess(len(text), 17000)

    def test_timeout_stops_hang_and_descendant_holding_pipe(self):
        for mode in ["hang", "descendant"]:
            with self.subTest(mode=mode), patch.dict(os.environ, {
                    "TEST_WORKER_MODE": mode, "VIDEO2DLSSNR_TIMEOUT": "0.4"}):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    self.run_video()

    def test_image_timeout_cleanup(self):
        with patch.dict(os.environ, {"TEST_WORKER_MODE": "hang", "VIDEO2DLSSNR_TIMEOUT": "0.4"}):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                nodes.run_image_np(self.frames()[0], 0, 1, [], str(self.exe))

    def test_runtime_check_reaches_worker_not_argument_typeerror(self):
        with patch.object(nodes, "find_exe", return_value=str(self.exe)):
            info = nodes.DLSSNRRuntimeInfo().info(0)[0]
        self.assertIn("neural rendering: OK", info)
        self.assertIn("fake Wine diagnostic", info)


if __name__ == "__main__":
    unittest.main()
