"""Opt-in real Wine cmd/CRT redirection integration, without NVIDIA or DLSS."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from test_wine_transport import nodes, runtime


SOURCE = r'''
#include <stdio.h>
#include <io.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
int main(int argc, char **argv) {
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
    fprintf(stderr, "REAL_WINE_STDERR: separate from raw frames\n");
    if (argc > 1 && strcmp(argv[1], "--diagnostics") == 0) {
        for (int i = 0; i < 100000; i++) {
            fputs("diagnostic stdout noise\n", stdout);
            fputs("diagnostic stderr noise\n", stderr);
        }
        fputs("DIAGNOSTICS_END", stdout); fputs("DIAGNOSTICS_END", stderr);
        return fflush(stdout) == 0 && fflush(stderr) == 0 ? 0 : 6;
    }
    const char *ready = getenv("V2D_TEST_READY");
    const char *late = getenv("V2D_TEST_LATE");
    if (ready && late) {
        FILE *f = fopen(ready, "wb"); if (!f) return 4; fclose(f);
        Sleep(2000);
        f = fopen(late, "wb"); if (!f) return 5; fclose(f);
    }
    unsigned char buffer[4096];
    size_t n;
    while ((n = fread(buffer, 1, sizeof(buffer), stdin)) > 0) {
        if (fwrite(buffer, 1, n, stdout) != n) return 2;
    }
    return fflush(stdout) == 0 ? 0 : 3;
}
'''


@unittest.skipUnless(os.environ.get("V2D_TEST_REAL_WINE") == "1",
                     "Set V2D_TEST_REAL_WINE=1 with Wine/Xvfb/MinGW installed")
class RealWineFileTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wine = shutil.which("wine")
        compiler = shutil.which("x86_64-w64-mingw32-gcc")
        if not (cls.wine and compiler and shutil.which("xvfb-run")):
            raise RuntimeError("Wine, Xvfb and x64 MinGW are required for this integration test")
        temp = tempfile.TemporaryDirectory(prefix="v2d_real_wine_", dir="/tmp")
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.prefix = cls.root / "prefix"
        cls.folder = cls.root / "worker with spaces"
        cls.folder.mkdir()
        source, cls.exe = cls.folder / "copy.c", cls.folder / "copy.exe"
        source.write_text(SOURCE)
        subprocess.run([compiler, "-O2", "-static", str(source), "-o", str(cls.exe)],
                       check=True, timeout=60)
        for leaf in ("nvngx_dlss.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll"):
            (cls.folder / leaf).write_bytes(b"MZtransport fixture; no GPU DLL is loaded")
        env = os.environ.copy()
        env.update({"WINEPREFIX": str(cls.prefix), "WINEARCH": "win64", "WINEDEBUG": "-all"})
        init = subprocess.run(["xvfb-run", "-a", cls.wine, "wineboot", "-u"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        if init.returncode:
            raise RuntimeError(init.stdout[-5000:].decode("utf-8", "replace"))
        cls.frames = np.arange(2 * 8 * 16 * 3, dtype=np.uint8).reshape(2, 8, 16, 3)

    def setUp(self):
        for context in (patch.object(runtime, "CONFIG_PATH", self.root / "no-config.json"),
                        patch.dict(os.environ, {"VIDEO2DLSSNR_WINE": self.wine,
                            "WINEPREFIX": str(self.prefix), "VIDEO2DLSSNR_XVFB": "1",
                            "VIDEO2DLSSNR_VIDEO_TRANSPORT": "files",
                            "VIDEO2DLSSNR_TIMEOUT": "90", "WINEDEBUG": "-all"})):
            context.start()
            self.addCleanup(context.stop)

    def test_binary_stdio_through_actual_wine_cmd(self):
        logs = []
        # Force the private batch directory itself to contain spaces.
        with patch.object(nodes.tempfile, "gettempdir", return_value=str(self.folder)):
            result = nodes.run_video_np(self.frames, 0, 1, [], False, "lk", False,
                                        str(self.exe), log=logs)
        np.testing.assert_array_equal(result, self.frames)
        self.assertIn("REAL_WINE_STDERR", "\n".join(logs))
        special_folder = self.root / "worker 50% & literal!"
        special_folder.mkdir()
        for source_file in self.folder.glob("*.dll"):
            shutil.copy2(source_file, special_folder / source_file.name)
        special_exe = special_folder / "copy.exe"
        shutil.copy2(self.exe, special_exe)
        result = nodes.run_video_np(self.frames, 0, 1, [], False, "lk", False, str(special_exe))
        np.testing.assert_array_equal(result, self.frames)

    def test_cancelled_windows_worker_cannot_continue(self):
        ready, late = self.root / "worker-ready", self.root / "escaped-worker"
        requested, stop = threading.Event(), threading.Event()
        observed = []
        def request_when_worker_is_running():
            while not stop.wait(0.05):
                if ready.exists():
                    observed.append(True)
                    requested.set()
                    return
        monitor = threading.Thread(target=request_when_worker_is_running, daemon=True)
        monitor.start()
        with patch.dict(os.environ, {"V2D_TEST_READY": runtime.windows_path(str(ready), self.prefix),
                                     "V2D_TEST_LATE": runtime.windows_path(str(late), self.prefix)}), patch.object(
                nodes, "cancellation_settings", return_value={"cancel_check": requested.is_set}):
            try:
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    nodes.run_video_np(self.frames, 0, 1, [], False, "lk", False, str(self.exe))
                self.assertTrue(observed, "cancellation must occur after the Windows worker starts")
                time.sleep(2.2)
                self.assertFalse(late.exists(), "cancelled Windows worker must not survive its launcher")
            finally:
                stop.set()
                monitor.join(timeout=1)

    def test_diagnostic_capture_is_bounded(self):
        p = runtime.RuntimeProcess(str(self.exe), ["--diagnostics"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = p.collect_output(limit=4096)
            p.check_timeout()
            self.assertEqual(p.returncode, 0)
            self.assertLessEqual(len(stdout), 4096)
            self.assertLessEqual(len(stderr), 4096)
            # Wine's console forwarding can merge and reorder the Windows
            # channels. Both emit the same end marker so whichever arrives
            # last has a verifiable terminal record in its bounded tail.
            tail = stdout + stderr
            self.assertIn(b"DIAGNOSTICS_END", tail, repr(tail[-512:]))
        finally:
            p.close_worker()


if __name__ == "__main__":
    unittest.main()
