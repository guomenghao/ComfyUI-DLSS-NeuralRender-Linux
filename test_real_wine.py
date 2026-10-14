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
        fputs("OUT_END", stdout); fputs("ERR_END", stderr);
        return 0;
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
    def test_binary_stdio_through_actual_wine_cmd(self):
        wine = shutil.which("wine")
        compiler = shutil.which("x86_64-w64-mingw32-gcc")
        self.assertTrue(wine and compiler and shutil.which("xvfb-run"),
                        "Wine, Xvfb and x64 MinGW are required for this integration test")
        with tempfile.TemporaryDirectory(prefix="v2d_real_wine_", dir="/tmp") as temp:
            root = Path(temp)
            prefix = root / "prefix"
            # Spaces in worker and batch paths also exercise Wine's argv quoting.
            folder = root / "worker with spaces"
            folder.mkdir()
            source, exe = folder / "copy.c", folder / "copy.exe"
            source.write_text(SOURCE)
            subprocess.run([compiler, "-O2", "-static", str(source), "-o", str(exe)],
                           check=True, timeout=60)
            for leaf in ("nvngx_dlss.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll"):
                (folder / leaf).write_bytes(b"MZtransport fixture; no GPU DLL is loaded")
            env = os.environ.copy()
            env.update({"WINEPREFIX": str(prefix), "WINEARCH": "win64", "WINEDEBUG": "-all"})
            init = subprocess.run(["xvfb-run", "-a", wine, "wineboot", "-u"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            self.assertEqual(init.returncode, 0, init.stdout[-5000:].decode("utf-8", "replace"))
            frames = np.arange(2 * 8 * 16 * 3, dtype=np.uint8).reshape(2, 8, 16, 3)
            logs = []
            with patch.object(runtime, "CONFIG_PATH", root / "no-config.json"), patch.dict(os.environ, {
                    "VIDEO2DLSSNR_WINE": wine, "WINEPREFIX": str(prefix),
                    "VIDEO2DLSSNR_XVFB": "1", "VIDEO2DLSSNR_VIDEO_TRANSPORT": "files",
                    "VIDEO2DLSSNR_TIMEOUT": "90", "WINEDEBUG": "-all"}):
                # Force the private batch directory itself to contain spaces.
                with patch.object(nodes.tempfile, "gettempdir", return_value=str(folder)):
                    result = nodes.run_video_np(frames, 0, 1, [], False, "lk", False,
                                                str(exe), log=logs)
                special_folder = root / "worker 50% & literal!"
                special_folder.mkdir()
                for source_file in folder.glob("*.dll"):
                    shutil.copy2(source_file, special_folder / source_file.name)
                special_exe = special_folder / "copy.exe"
                shutil.copy2(exe, special_exe)
                special_result = nodes.run_video_np(frames, 0, 1, [], False, "lk", False, str(special_exe))
                np.testing.assert_array_equal(special_result, frames)
                # Exercise the actual Wine process, not just a Python mock.
                p = runtime.RuntimeProcess(str(exe), ["--diagnostics"],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = p.collect_output(limit=4096)
                    p.check_timeout()
                    self.assertEqual(p.returncode, 0)
                    self.assertLessEqual(len(stdout), 4096)
                    self.assertLessEqual(len(stderr), 4096)
                    self.assertTrue(stdout.endswith(b"OUT_END"))
                    self.assertTrue(stderr.endswith(b"ERR_END"))
                finally:
                    p.close_worker()

                ready, late = root / "worker-ready", root / "escaped-worker"
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
                with patch.dict(os.environ, {
                        "V2D_TEST_READY": runtime.windows_path(str(ready), prefix),
                        "V2D_TEST_LATE": runtime.windows_path(str(late), prefix)}), patch.object(
                        nodes, "cancellation_settings", return_value={"cancel_check": requested.is_set}):
                    try:
                        with self.assertRaisesRegex(RuntimeError, "cancelled"):
                            nodes.run_video_np(frames, 0, 1, [], False, "lk", False, str(exe))
                        self.assertTrue(observed, "cancellation must occur after the Windows worker starts")
                        time.sleep(2.2)
                        self.assertFalse(late.exists(), "cancelled Windows worker must not survive its launcher")
                    finally:
                        stop.set()
                        monitor.join(timeout=1)
            np.testing.assert_array_equal(result, frames)
            self.assertIn("REAL_WINE_STDERR", "\n".join(logs))


if __name__ == "__main__":
    unittest.main()
