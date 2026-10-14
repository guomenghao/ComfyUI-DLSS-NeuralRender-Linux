"""Linux control-script integration with a synthetic HTTP server and Xvfb shim.

No ComfyUI, GPU or real display is used. Only test-owned processes are stopped.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "start.sh").read_text().split("# PYTHON CONTROL BEGIN\n", 1)[1].split("# PYTHON CONTROL END", 1)[0]
controller = type(sys)("v2d_test_controller")
exec(compile(SOURCE, str(ROOT / "start.sh"), "exec"), controller.__dict__)

SERVER = '''
import argparse, json, os, signal
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
p = argparse.ArgumentParser()
p.add_argument('--port',type=int)
args,_ = p.parse_known_args()
if os.environ.get('CONTROL_TEST_IGNORE_TERM') == '1':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path('ready.json').write_text(json.dumps({'pid':os.getpid(), 'display':os.environ.get('DISPLAY'), 'args':__import__('sys').argv}))
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/interrupt': Path('interrupt.received').touch()
        self.send_response(200); self.end_headers()
    def log_message(self,*args): pass
class Server(HTTPServer): allow_reuse_address = True
Server(('127.0.0.1',args.port),Handler).serve_forever()
'''


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProcessIdentificationTests(unittest.TestCase):
    def test_stop_does_not_pass_its_mode_argument_to_conda_activation(self):
        with tempfile.TemporaryDirectory(prefix="v2d_activation_") as temp:
            root = Path(temp)
            activate = root / "activate"
            marker = root / "activation-args"
            activate.write_text('printf "%s\\n" "$*" > "' + str(marker) +
                                '"\n[ "$#" = 1 ] && [ "$1" = base ] || return 7\n')
            script = root / "start.sh"
            text = (ROOT / "start.sh").read_text().replace('/root/miniconda3/bin/python', sys.executable)
            text = text.replace('/root/miniconda3/bin/activate', str(activate))
            script.write_text(text)
            result = subprocess.run(["bash", str(script), "--stop"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=5)
            self.assertEqual(marker.read_text().strip(), "base")
            self.assertNotEqual(result.returncode, 7, result.stdout)

    def test_older_kernel_pidfd_falls_back_after_identity_check(self):
        import errno
        record = {"pid": 43210}
        with patch.object(controller.os, "pidfd_open", create=True,
                          side_effect=OSError(errno.ENOSYS, "synthetic unsupported syscall")), patch.object(
                controller.signal, "pidfd_send_signal", create=True), patch.object(
                controller, "same_process", return_value=True), patch.object(controller.os, "kill") as kill:
            controller.send_signal(record, signal.SIGTERM)
            kill.assert_called_once_with(43210, signal.SIGTERM)

    def test_argument_position_cwd_and_port_must_identify_actual_main(self):
        directory = Path("/tmp/synthetic ComfyUI").resolve()
        info = {"args": ["python", "-u", "main.py", "--port", "8188"], "cwd": str(directory)}
        self.assertTrue(controller.worker_matches(info, directory, 8188))
        self.assertFalse(controller.worker_matches(info, directory, 8189))
        for args in (["python", "other.py", "main.py", "--port", "8188"],
                     ["python", "-c", "main.py", "--port", "8188"],
                     ["sh", "xvfb-run", "python", "main.py", "--port", "8188"]):
            self.assertFalse(controller.worker_matches({**info, "args": args}, directory, 8188))
        self.assertFalse(controller.worker_matches({**info, "cwd": "/tmp/other"}, directory, 8188))


@unittest.skipUnless(sys.platform == "linux", "Live process tests require Linux /proc")
class ControlScriptIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="v2d_control_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = self.root / "ComfyUI with spaces"
        self.app.mkdir()
        for leaf in ("start.sh", "stop.sh"):
            shutil.copy2(ROOT / leaf, self.app / leaf)
        (self.app / "main.py").write_text(SERVER)
        bins = self.root / "bin"
        bins.mkdir()
        # Retain a shell launcher like xvfb-run, while using no real X server.
        (bins / "xvfb-run").write_text('#!/bin/sh\nshift\nexport DISPLAY=:199\n"$@"\n')
        (bins / "xauth").write_text('#!/bin/sh\nexit 0\n')
        for file in bins.iterdir(): file.chmod(0o755)
        self.port = unused_port()
        self.env = os.environ.copy()
        self.env.update(V2D_COMFY_PYTHON=sys.executable, V2D_COMFY_PORT=str(self.port),
                        V2D_COMFY_STOP_TIMEOUT="1", PATH=str(bins) + os.pathsep + self.env["PATH"])
        self.addCleanup(self.cleanup_app)

    def cleanup_app(self):
        # /proc scoping limits cleanup to the synthetic app and selected port.
        subprocess.run(["bash", str(self.app / "stop.sh")], env=self.env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12)

    def control(self, name, good=True):
        result = subprocess.run(["bash", str(self.app / name)], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=12)
        if good: self.assertEqual(result.returncode, 0, result.stdout)
        return result

    def wait_ready(self, app=None):
        app = app or self.app
        deadline = time.monotonic() + 5
        while not (app / "ready.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((app / "ready.json").exists())
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("Synthetic HTTP server did not listen in time")

    def test_start_duplicate_stop_and_immediate_restart(self):
        import json
        self.control("start.sh")
        self.wait_ready()
        ready = json.loads((self.app / "ready.json").read_text())
        self.assertEqual(ready["display"], ":199")
        self.assertIn("--enable-manager", ready["args"])
        pids = [i["pid"] for i in controller.workers(self.app.resolve(), self.port)]
        self.assertEqual(len(pids), 1)
        self.control("start.sh")
        self.assertEqual([i["pid"] for i in controller.workers(self.app.resolve(), self.port)], pids)
        self.control("stop.sh")
        self.assertEqual(controller.workers(self.app.resolve(), self.port), [])
        self.assertTrue((self.app / "interrupt.received").exists())
        (self.app / "ready.json").unlink()
        self.control("start.sh")
        self.wait_ready()
        self.assertNotEqual([i["pid"] for i in controller.workers(self.app.resolve(), self.port)], pids)

    def test_stop_external_instance_preserves_different_port(self):
        port2 = unused_port()
        other = subprocess.Popen([sys.executable, "main.py", "--port", str(port2)], cwd=self.app,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def reap_other():
            if other.poll() is None: other.kill()
            other.wait(timeout=3)
        self.addCleanup(reap_other)
        target = subprocess.Popen([sys.executable, "main.py", "--port", str(self.port)], cwd=self.app,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_ready()
            self.control("stop.sh")
            target.wait(timeout=3)
            self.assertIsNone(other.poll())
        finally:
            if target.poll() is None: target.kill()
            target.wait(timeout=3)
            if other.poll() is None: other.kill()
            other.wait(timeout=3)

    def test_stale_state_cannot_stop_launcher_on_a_different_port(self):
        port2 = unused_port()
        other = subprocess.Popen([str(self.root / "bin/xvfb-run"), "-a", sys.executable,
            "main.py", "--port", str(port2)], cwd=self.app, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not controller.workers(self.app.resolve(), port2) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(controller.workers(self.app.resolve(), port2))
            record = controller.process_info(other.pid)
            runtime = self.app / ".comfyui-control"
            runtime.mkdir()
            controller.write_state(runtime / f"{self.port}.json", record)
            self.control("stop.sh")
            self.assertIsNone(other.poll())
            self.assertTrue(controller.workers(self.app.resolve(), port2))
        finally:
            try: os.killpg(other.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            other.wait(timeout=3)

    def test_busy_port_refuses_start_and_does_not_stop_other_service(self):
        with socket.socket() as other:
            other.bind(("127.0.0.1", self.port))
            other.listen()
            result = self.control("start.sh", good=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("端口", result.stdout)
            self.control("stop.sh")
            self.assertNotEqual(other.fileno(), -1)

    def test_term_resistant_owned_instance_is_stopped(self):
        self.env["CONTROL_TEST_IGNORE_TERM"] = "1"
        self.control("start.sh")
        self.wait_ready()
        result = self.control("stop.sh")
        self.assertIn("正常退出超时", result.stdout)
        self.assertEqual(controller.workers(self.app.resolve(), self.port), [])


if __name__ == "__main__":
    unittest.main()
