"""Experimental Wine transport; no change to the upstream NVIDIA/NR binaries.

Only worker subprocesses receive prefix and DLL settings. This module has no
ComfyUI, torch or numpy dependency, so preflight can run before node import.
"""
from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "linux-runtime.json"
ENV_KEYS = frozenset({
    "VIDEO2DLSSNR_WINE", "WINEPREFIX", "WINESERVER", "WINEDEBUG",
    "WINEDLLOVERRIDES", "WINE_HEAP_DELAY_FREE", "WINE_HEAP_ZERO_MEMORY",
    "DXVK_ENABLE_NVAPI", "DXVK_CONFIG", "DXVK_FILTER_DEVICE_NAME",
    "VKD3D_FILTER_DEVICE_NAME", "NVIDIA_WINE_DLL_DIR",
    "VIDEO2DLSSNR_XVFB", "WINE_GRAPHICS_DRIVER",
    "VIDEO2DLSSNR_VIDEO_TRANSPORT",
})
PATH_FLAGS = frozenset({"--in", "--out", "--dll-dir", "--json"})


def _timeout(value: object) -> float:
    try:
        seconds = float(value)
        if isinstance(value, bool) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError
        return seconds
    except (ValueError, TypeError) as exc:
        raise RuntimeError("VIDEO2DLSSNR_TIMEOUT / timeout_seconds must be a positive finite number.") from exc


def worker_settings() -> tuple[dict[str, str], float]:
    env = os.environ.copy()
    seconds = env.get("VIDEO2DLSSNR_TIMEOUT", "1800")
    if sys.platform != "linux":
        return env, _timeout(seconds)
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return env, _timeout(seconds)
    except OSError as exc:
        raise RuntimeError(f"Cannot read {CONFIG_PATH}: {exc}") from exc
    try:
        cfg = json.loads(raw)
        if not isinstance(cfg, dict) or type(cfg.get("version")) is not int or cfg["version"] != 1:
            raise ValueError("expected version 1")
        if set(cfg) - {"version", "environment", "timeout_seconds"}:
            raise ValueError("unknown config fields")
        overrides = cfg["environment"]
        if not isinstance(overrides, dict):
            raise ValueError("environment must be an object")
        for key, value in overrides.items():
            if key not in ENV_KEYS or not isinstance(value, str) or "\0" in value:
                raise ValueError(f"invalid environment setting: {key}")
        env.update(overrides)
        seconds = _timeout(cfg.get("timeout_seconds", seconds))
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid {CONFIG_PATH}: {exc}") from exc
    return env, seconds


def validate_binary(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
    except OSError as exc:
        raise RuntimeError(f"Missing/unreadable runtime: {path}") from exc
    if header.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise RuntimeError(f"Runtime is a Git LFS pointer, not the actual binary: {path}")
    if not header.startswith(b"MZ"):
        raise RuntimeError(f"Not a Windows PE runtime: {path}")


def windows_path(path: str, prefix: Path) -> str:
    """Map a host path through THIS prefix's actual dosdevices links.

    Do not assume Z: points at /. Image outputs may not exist yet. No shell,
    winepath process, or temporary prefix initialization is needed.
    """
    target = Path(path).expanduser().resolve()
    mappings = []
    for drive in (prefix / "dosdevices").iterdir():
        if not re.fullmatch(r"[a-zA-Z]:", drive.name) or not drive.is_symlink():
            continue
        mount = drive.resolve()
        if not mount.is_dir():
            continue
        try:
            relative = target.relative_to(mount)
        except ValueError:
            continue
        mappings.append((len(mount.parts), drive.name.upper(), relative))
    if not mappings:
        raise RuntimeError(f"Path is not accessible through this Wine prefix's drive mappings: {target}")
    _, letter, relative = max(mappings, key=lambda item: (item[0], item[1]))
    return letter + "\\" + "\\".join(relative.parts)


def launch_spec(exe: str, args: list[str], *, program: str | None = None) -> tuple[list[str], dict, float]:
    binary = Path(exe).expanduser().resolve()
    for leaf in (binary.name, "nvngx_dlss.dll", "nvngx_dlssnr.dll", "nvngx.dll_dlssnr.dll"):
        validate_binary(binary.parent / leaf)
    env, timeout = worker_settings()
    if sys.platform == "win32":
        if program is not None:
            raise RuntimeError("The Wine command wrapper is Linux-only.")
        return [str(binary), *args], {
            "cwd": str(binary.parent), "env": env,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        }, timeout
    if sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("video2dlssnr requires Windows or Linux x86-64 with a configured Wine runtime.")
    prefix_raw = env.get("WINEPREFIX", "")
    prefix = Path(prefix_raw).expanduser()
    if not prefix_raw or not prefix.is_absolute():
        raise RuntimeError("Set WINEPREFIX to an absolute path for an initialized dedicated prefix; see LINUX.md.")
    if not (prefix / "drive_c/windows/system32").is_dir() or not (prefix / "dosdevices").is_dir():
        raise RuntimeError("WINEPREFIX is not initialized. Configure Wine, VKD3D-Proton and DXVK-NVAPI first.")
    env["WINEPREFIX"] = str(prefix.resolve())
    configured = env.get("VIDEO2DLSSNR_WINE", "")
    wine = shutil.which(configured) if configured else (shutil.which("wine64") or shutil.which("wine"))
    if not wine:
        raise RuntimeError("Wine not found. VIDEO2DLSSNR_WINE must name one executable, not a shell command or Proton launcher.")
    converted = list(args)
    for index, arg in enumerate(converted):
        if arg in PATH_FLAGS:
            if index + 1 >= len(converted):
                raise RuntimeError(f"Missing path argument after {arg}")
            converted[index + 1] = windows_path(converted[index + 1], prefix)
    # Native NVAPI is needed to expose NVIDIA. Existing explicit settings win.
    env.setdefault("WINEDEBUG", "-all")
    env.setdefault("DXVK_ENABLE_NVAPI", "1")
    if program not in (None, "cmd.exe"):
        raise RuntimeError("Unsupported Wine command wrapper.")
    command = [wine, program or windows_path(str(binary), prefix), *converted]
    headless = env.get("VIDEO2DLSSNR_XVFB", "0")
    if headless not in {"0", "1"}:
        raise RuntimeError("VIDEO2DLSSNR_XVFB must be 0 or 1.")
    if headless == "1":
        xvfb = shutil.which("xvfb-run")
        if not xvfb:
            raise RuntimeError("Headless Wine needs xvfb-run and xauth; install xvfb and xauth.")
        command = [xvfb, "-a", "--server-args=-screen 0 1280x720x24 -nolisten tcp", *command]
    return command, {
        "cwd": str(binary.parent), "env": env, "start_new_session": True,
    }, timeout


class RuntimeProcess(subprocess.Popen):
    """Bound the whole job, including stalled reads, and own only its process group."""

    def __init__(self, exe: str, args: list[str], *, program: str | None = None,
                 cancel_check=None, cancel_error=None, **kwargs):
        command, options, self.timeout_seconds = launch_spec(exe, args, program=program)
        self._owns_group = bool(options.get("start_new_session"))
        self.timed_out = threading.Event()
        self.cancelled = threading.Event()
        self._stop_monitor = threading.Event()
        self._cancel_error = cancel_error
        self._monitor_error = None
        self._timeout_lock = threading.Lock()
        self._closed = False
        try:
            super().__init__(command, **options, **kwargs)
        except OSError as exc:
            raise RuntimeError(f"Cannot start video2dlssnr worker: {exc}") from exc
        self._timer = threading.Timer(self.timeout_seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()
        if cancel_check is not None:
            self._monitor = threading.Thread(target=self._watch_cancel, args=(cancel_check,), daemon=True)
            self._monitor.start()

    def _watch_cancel(self, check):
        while not self._stop_monitor.wait(0.1):
            try:
                cancelled = check()
            except Exception as exc:
                self._monitor_error = exc
                cancelled = True
            if cancelled:
                with self._timeout_lock:
                    if not self._closed:
                        self.cancelled.set()
                        self.abort()
                return

    def _on_timeout(self):
        with self._timeout_lock:
            if not self._closed:
                self.timed_out.set()
                self.abort()

    def abort(self):
        # Never kill the shared wineserver or an unrelated render/prefix client.
        try:
            if self._owns_group:
                os.killpg(self.pid, signal.SIGKILL)
            elif self.poll() is None:
                super().kill()
        except ProcessLookupError:
            pass

    def check_timeout(self):
        if self._monitor_error is not None:
            raise RuntimeError("Cannot check ComfyUI task cancellation") from self._monitor_error
        if self.cancelled.is_set():
            if self._cancel_error is not None:
                raise self._cancel_error()
            raise RuntimeError("video2dlssnr task cancelled.")
        if self.timed_out.is_set():
            raise RuntimeError(f"video2dlssnr timed out after {self.timeout_seconds:g}s. See Wine/NGX logs; VIDEO2DLSSNR_TIMEOUT can be increased for long jobs.")

    def collect_output(self, limit=65536):
        """Drain both diagnostic pipes concurrently, retaining a bounded tail.

        Only for jobs whose input is inherited or DEVNULL. Frame pipes use their
        own reader/writer; communicate() would retain unlimited diagnostic data.
        """
        if self.stdin is not None or not isinstance(limit, int) or limit <= 0:
            raise ValueError("collect_output requires no stdin pipe and a positive tail limit")
        empty = "" if self.text_mode else b""
        tails = [empty, empty]
        errors = []

        def drain(index, stream):
            if stream is None:
                return
            try:
                for chunk in iter(lambda: stream.read(8192), empty):
                    tails[index] = (tails[index] + chunk)[-limit:]
            except (OSError, ValueError) as exc:
                errors.append(exc)
                self.abort()

        threads = [threading.Thread(target=drain, args=(index, stream), daemon=True)
                   for index, stream in enumerate((self.stdout, self.stderr))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()  # timeout/cancellation also kills descendants holding these pipes
        self.wait()
        if errors:
            self.check_timeout()
            raise RuntimeError("Cannot read video2dlssnr diagnostic output") from errors[0]
        return tuple(tails)

    def close_worker(self):
        with self._timeout_lock:
            self._closed = True
            self._timer.cancel()
            self._stop_monitor.set()
        # A parent can already have exited while its children are still alive.
        if self._owns_group or self.poll() is None:
            self.abort()
        self.wait(timeout=5)
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None:
                stream.close()
