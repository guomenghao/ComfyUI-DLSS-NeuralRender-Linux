"""Check launcher without torch; --help-worker and --probe-nr are opt-in.

This never installs packages, initializes a prefix, copies DLLs or changes
driver settings. The worker itself can write logs/NGX cache into its prefix.
"""
import argparse
import json
import os
import sys
from pathlib import Path

from wine_runtime import RuntimeProcess, launch_spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--help-worker", action="store_true", help="Actually launch the worker's --help; does not test DLSS")
    mode.add_argument("--probe-nr", action="store_true", help="Run upstream production --probe-nr; creates an actual GPU feature")
    parser.add_argument("--adapter", type=int, default=0)
    args = parser.parse_args()
    exe = os.environ.get("VIDEO2DLSSNR_EXE") or str(Path(__file__).resolve().parent / "bin/video2dlssnr.exe")
    try:
        flags = ["--probe-nr", "--nr-in", "1280x720", "--nr-out", "1280x720",
                 "--adapter", str(args.adapter)] if args.probe_nr else ["--help"]
        command, options, timeout = launch_spec(exe, flags)
        print(json.dumps({"status": "launcher configuration valid; GPU not yet verified",
            "command": command, "cwd": options["cwd"], "timeout_seconds": timeout,
            "prefix": options["env"].get("WINEPREFIX"),
            "platform": sys.platform}, ensure_ascii=False, indent=2), flush=True)
        if args.help_worker or args.probe_nr:
            worker = RuntimeProcess(exe, flags, stdout=-1, stderr=-1,
                                    text=True, encoding="utf-8", errors="replace")
            try:
                stdout, stderr = worker.communicate()
                worker.check_timeout()
                print(stdout or "", end="")
                print(stderr or "", end="", file=sys.stderr)
                if worker.returncode != 0:
                    return worker.returncode if worker.returncode > 0 else 1
                print("Worker exited successfully. Validate real image/video outputs in ComfyUI before claiming Linux DLSS works.")
            finally:
                worker.close_worker()
        return 0
    except RuntimeError as exc:
        print("FAILED: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
