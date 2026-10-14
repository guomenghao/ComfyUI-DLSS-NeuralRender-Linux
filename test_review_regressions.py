"""Review regressions: metadata, invalid inputs, bounded logs and cancellation.

These exercise Python behavior and real local subprocesses, not NVIDIA/NR.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

from test_wine_transport import nodes, runtime
from test_deploy_helpers import helper
from test_deploy_helpers import pe


class MetadataTests(unittest.TestCase):
    def test_in_memory_hdr_does_not_encode_a_stream_to_probe(self):
        for color, expected in [("HDR PQ", "pq"), ("HDR", "hlg"), ("sRGB", None)]:
            video = Mock()
            video.get_color_space.return_value = color
            video.get_stream_source.side_effect = AssertionError("must not serialize a VIDEO")
            self.assertEqual(nodes.probe_transfer(video), expected)
            video.get_stream_source.assert_not_called()

    def test_legacy_stream_probe_restores_original_cursor(self):
        source = io.BytesIO(b"fixture")
        source.seek(3)
        video = types.SimpleNamespace(get_stream_source=lambda: source)
        container = Mock()
        container.streams.video = [types.SimpleNamespace(
            codec_context=types.SimpleNamespace(color_trc=16))]
        context = Mock()
        context.__enter__ = Mock(return_value=container)
        context.__exit__ = Mock(return_value=False)
        with patch.dict(sys.modules, {"av": types.SimpleNamespace(open=lambda src: context)}):
            self.assertEqual(nodes.probe_transfer(video), "pq")
        self.assertEqual(source.tell(), 3)

    def test_hdr_never_silently_falls_back_to_sdr_constructor(self):
        calls = []
        def legacy(comps):
            calls.append(comps)
            return "SDR output"
        modules = {"comfy_api": types.ModuleType("comfy_api"),
                   "comfy_api.input_impl": types.SimpleNamespace(VideoFromComponents=legacy),
                   "comfy_api.util": types.SimpleNamespace(VideoComponents=lambda **kwargs: kwargs)}
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, "HDR"):
                nodes.make_video("frames", None, 24, "pq")
        self.assertEqual(calls, [])

    def test_hdr_video_metadata_audio_and_fractional_rate_are_preserved(self):
        audio = object()
        modules = {"comfy_api": types.ModuleType("comfy_api"),
                   "comfy_api.input_impl": types.SimpleNamespace(
                       VideoFromComponents=lambda comps, **kw: (comps, kw)),
                   "comfy_api.util": types.SimpleNamespace(VideoComponents=lambda **kw: kw)}
        with patch.dict(sys.modules, modules):
            comps, kw = nodes.make_video("frames", audio, nodes.Fraction(24000, 1001), "hlg")
        self.assertIs(comps["audio"], audio)
        self.assertEqual(comps["frame_rate"], nodes.Fraction(24000, 1001))
        self.assertEqual(kw, {"bit_depth": 10, "color_space": "HDR"})


class FrameValidationTests(unittest.TestCase):
    def test_invalid_frames_and_transfer_fail_before_worker_launch(self):
        cases = [(np.zeros((1, 4, 6, 3), np.float32), None),
                 (np.zeros((1, 4, 6, 4), np.uint8), None),
                 (np.zeros((1, 0, 6, 3), np.uint8), None),
                 (np.zeros((0, 4, 6, 3), np.uint8), None),
                 (np.zeros((1, 4, 6, 3), np.uint16), "unknown"),
                 (np.zeros((1, 4, 6, 3), np.uint8), "pq")]
        with patch.object(nodes, "RuntimeProcess", side_effect=AssertionError("invalid input launched")):
            for frames, transfer in cases:
                with self.subTest(shape=frames.shape, dtype=frames.dtype, transfer=transfer):
                    with self.assertRaises(ValueError):
                        nodes.run_video_np(frames, 0, 1, [], False, "lk", False,
                                           "unused.exe", transfer=transfer)

    def test_bad_dimensions_fail_before_worker_launch(self):
        frames = np.zeros((1, 4, 6, 3), np.uint8)
        for width, scale, height in [(0, float("nan"), 0), (-2, 1, 0), (0, 0, 0), (0, 1, -4)]:
            with self.subTest(width=width, scale=scale, height=height), patch.object(
                    nodes, "RuntimeProcess", side_effect=AssertionError("invalid size launched")):
                with self.assertRaises(ValueError):
                    nodes.run_video_np(frames, width, scale, [], False, "lk", False,
                                       "unused.exe", height=height)


class ProcessLifecycleTests(unittest.TestCase):
    def launch(self, script, **kwargs):
        return runtime.RuntimeProcess("unused", [], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, **kwargs)

    def spec(self, script, timeout=3):
        return ([sys.executable, "-c", script], {"start_new_session": True}, timeout)

    def test_large_stdout_stderr_are_drained_and_only_tails_retained(self):
        script = "import sys; sys.stdout.buffer.write(b'A'*5000000+b'OUT_END'); sys.stderr.buffer.write(b'B'*5000000+b'ERR_END')"
        with patch.object(runtime, "launch_spec", return_value=self.spec(script)):
            p = self.launch(script)
            try:
                stdout, stderr = p.collect_output(limit=4096)
                self.assertEqual(p.returncode, 0)
                self.assertLessEqual(len(stdout), 4096)
                self.assertLessEqual(len(stderr), 4096)
                self.assertTrue(stdout.endswith(b"OUT_END"))
                self.assertTrue(stderr.endswith(b"ERR_END"))
            finally:
                p.close_worker()

    def test_cancellation_stops_blocked_worker_and_propagates_base_exception(self):
        class Interrupted(BaseException):
            pass
        cancelled = threading.Event()
        script = "import time; time.sleep(60)"
        with patch.object(runtime, "launch_spec", return_value=self.spec(script, 15)):
            p = self.launch(script, cancel_check=cancelled.is_set, cancel_error=Interrupted)
            timer = threading.Timer(0.2, cancelled.set)
            timer.start()
            started = time.monotonic()
            try:
                p.collect_output()
                with self.assertRaises(Interrupted):
                    p.check_timeout()
                self.assertLess(time.monotonic() - started, 3)
            finally:
                timer.cancel()
                p.close_worker()

    def test_closing_exited_parent_still_stops_owned_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / "descendant-survived"
            child = f"import time,pathlib; time.sleep(0.6); pathlib.Path({str(marker)!r}).touch()"
            script = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
            with patch.object(runtime, "launch_spec", return_value=self.spec(script)):
                p = self.launch(script)
                p.wait()
                p.close_worker()
            time.sleep(0.9)
            self.assertFalse(marker.exists(), "closing must reap children even after parent exits")


class ConfigurationTests(unittest.TestCase):
    def test_extraction_retry_preserves_old_files_and_failed_attempt_does_not_promote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "driver"
            target.mkdir()
            (target / "original-partial").write_text("retained")
            archive = root / "fixture.run"
            archive.write_text('test "$1" = --extract-only || exit 99\nmkdir -p "$3"\ncp "' +
                               str(root / "bridge") + '" "$3/_nvngx.dll"\n')
            (root / "bridge").write_bytes(pe(0x8664))
            helper.extract_driver(archive, target)
            self.assertTrue(helper.pe64(target / "_nvngx.dll"))
            backups = list(root.glob("driver-incomplete-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "original-partial").read_text(), "retained")
            archive.write_text('test "$1" = --extract-only || exit 99\nmkdir -p "$3"\nexit 7\n')
            with self.assertRaisesRegex(RuntimeError, "exit 7"):
                helper.extract_driver(archive, target)
            self.assertTrue(helper.pe64(target / "_nvngx.dll"))
            self.assertEqual(list(root.glob("nvidia-extract-*")), [])

    def test_saved_timeout_survives_reconfiguration_and_invalid_timeout_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            node = Path(temp)
            self.assertEqual(helper.saved_timeout(node), 600)
            cfg = node / "linux-runtime.json"
            cfg.write_text(json.dumps({"version": 1, "timeout_seconds": 7200, "environment": {}}))
            self.assertEqual(helper.saved_timeout(node), 7200)
            cfg.write_text(json.dumps({"version": 1, "timeout_seconds": float("nan"), "environment": {}}))
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                helper.saved_timeout(node)

    def test_saved_timeout_wins_over_invalid_shell_default(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = Path(temp) / "config.json"
            cfg.write_text(json.dumps({"version": 1, "timeout_seconds": 100, "environment": {}}))
            with patch.object(runtime.sys, "platform", "linux"), patch.object(runtime, "CONFIG_PATH", cfg), patch.dict(
                    os.environ, {"VIDEO2DLSSNR_TIMEOUT": "invalid"}):
                self.assertEqual(runtime.worker_settings()[1], 100)


if __name__ == "__main__":
    unittest.main()
