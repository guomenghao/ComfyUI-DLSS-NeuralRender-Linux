"""Exercise real curl against local HTTP Range and interrupted transfers."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import unittest

from fetch_asset import fetch


DATA = bytes(range(256)) * 8192
DIGEST = hashlib.sha256(DATA).hexdigest()


@unittest.skipUnless(shutil.which("curl"), "curl required")
class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "asset.tar.gz"
        self.partial = Path(str(self.target) + ".part")
        self.requests = []
        self.fail_first = False
        self.ignore_range = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                header = self.headers.get("Range")
                test.requests.append(header)
                offset = int(header.split("=")[1].split("-")[0]) if header else 0
                if test.ignore_range:
                    offset = 0
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(DATA) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(DATA)-1}/{len(DATA)}")
                self.end_headers()
                if test.fail_first and len(test.requests) == 1:
                    self.wfile.write(DATA[offset:offset + 65536])
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_WR)
                    self.close_connection = True
                else:
                    try:
                        self.wfile.write(DATA[offset:])
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/asset"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_existing_partial_resumes_and_verifies(self):
        self.partial.write_bytes(DATA[:77777])
        fetch(self.url, self.target, DIGEST)
        self.assertEqual(self.requests, ["bytes=77777-"])
        self.assertEqual(self.target.read_bytes(), DATA)
        self.assertFalse(self.partial.exists())

    def test_retry_resumes_new_bytes_after_dropped_connection(self):
        self.fail_first = True
        fetch(self.url, self.target, DIGEST, retry_delay=0)
        self.assertEqual(self.requests, [None, "bytes=65536-"])
        self.assertEqual(self.target.read_bytes(), DATA)

    def test_source_ignoring_range_preserves_partial(self):
        self.ignore_range = True
        original = DATA[:77777]
        self.partial.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "cannot resume"):
            fetch(self.url, self.target, DIGEST, retry_delay=0)
        self.assertEqual(self.partial.read_bytes(), original)
        self.assertFalse(self.target.exists())

    def test_complete_partial_and_cache_do_not_download(self):
        self.partial.write_bytes(DATA)
        fetch(self.url, self.target, DIGEST)
        fetch(self.url, self.target, DIGEST)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.target.read_bytes(), DATA)

    def test_checksum_mismatch_preserves_download(self):
        with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
            fetch(self.url, self.target, "0" * 64)
        self.assertEqual(self.partial.read_bytes(), DATA)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
