"""ComfyUI nodes for video2dlssnr — DLSS Super Resolution + Neural Rendering.

Two nodes, mirroring the two tabs of the Gradio UI:

  * DLSS Neural Rendering (Image)  — every image in the batch independently
                                     (video2dlssnr.exe --nr-run, same as the Image tab).
  * DLSS Neural Rendering (Video)  — the batch is a clip: frames are streamed through
                                     --nr-video with optical-flow motion vectors, so NR stays
                                     temporally stable. Frames go to the exe as raw RGBA over a
                                     pipe and come back the same way on Windows. Linux can
                                     isolate Wine diagnostics using private raw frame files.
                                     HDR clips (PQ / HLG) are recognised from the Load Video
                                     source and take the tool's HDR path: 16-bit frames, an SDR
                                     proxy for the model, the edit back on the HDR frame as a
                                     linear-light residual; the VIDEO output is tagged 10-bit
                                     HDR PQ / HLG the way the core Create Video node does.

video2dlssnr.exe (with its runtime DLLs next to it) is looked up in this order:
  1. the VIDEO2DLSSNR_EXE environment variable — full path to video2dlssnr.exe;
  2. <this folder>/bin/video2dlssnr.exe.

Experimental Linux x86-64 support launches only this worker through Wine.
See LINUX.md for prefix, graphics libraries, and acceptance tests.
"""

import glob
import math
from collections import deque
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from fractions import Fraction
from pathlib import Path

from .wine_runtime import RuntimeProcess, launch_spec, worker_settings, windows_path

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))

STYLES = {"Default": 0, "Natural": 1, "Cinematic": 2}
PRESETS = {"Default": 0, "Preset 1": 1, "Preset 2": 2, "Preset 3": 3}
ENGINES = ["auto", "nvof", "lk"]
SR_PRESETS = ["Default", "E", "F", "J", "K", "L", "M"]
# How the clip is coded. auto reads the transfer characteristic of the connected VIDEO's source
# stream (an IMAGE batch carries none and counts as SDR); the rest override it.
HDR_SOURCES = ["auto", "sdr", "pq", "hlg"]
# ffmpeg / PyAV transfer characteristic -> the tool's --nr-transfer, and the ComfyUI colour space
# name the core Create Video / Save Video nodes use for it.
AV_TRC_SMPTE2084, AV_TRC_ARIB_STD_B67 = 16, 18
TRC_TO_TRANSFER = {"smpte2084": "pq", "arib-std-b67": "hlg",
                   AV_TRC_SMPTE2084: "pq", AV_TRC_ARIB_STD_B67: "hlg"}
COMFY_COLOR_SPACE = {"pq": "HDR PQ", "hlg": "HDR"}


# ----------------------------------------------------------------------------- tool lookup

def find_exe():
    env = os.environ.get("VIDEO2DLSSNR_EXE", "").strip().strip('"')
    cands = [env] if env else []
    cands.append(os.path.join(HERE, "bin", "video2dlssnr.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError(
        "video2dlssnr.exe not found. Set VIDEO2DLSSNR_EXE to its full path, or copy the out\\ "
        "folder of a video2dlssnr release into: " + os.path.join(HERE, "bin"))


def nr_args(style, preset, intensity, local_structure, local_tone, skin, detail,
            color, ui_correction, auto_mask, sr_preset="Default"):
    """NR model + composite flags, named exactly like the CLI (--nr-*)."""
    a = ["--nr-sr-preset", "default" if sr_preset == "Default" else sr_preset,
         "--nr-style", str(STYLES[style]), "--nr-preset", str(PRESETS[preset]),
         "--nr-intensity", f"{intensity}", "--nr-local-structure", f"{local_structure}",
         "--nr-local-tone", f"{local_tone}", "--nr-skin", f"{skin}", "--nr-detail", f"{detail}",
         "--nr-color", f"{color}", "--nr-ui-correction", "1" if ui_correction else "0"]
    if auto_mask:
        a.append("--nr-auto-mask")
    return a


def probe_transfer(video):
    """'pq' / 'hlg' when the VIDEO input's source stream is tagged HDR, else None.

    Prefer ComfyUI metadata, including in-memory VIDEO inputs. Older APIs fall
    back to PyAV stream tags. A failed legacy probe requires an explicit HDR
    override rather than silently treating an unknown stream as SDR.
    """
    get_color_space = getattr(video, "get_color_space", None)
    if callable(get_color_space):
        color = get_color_space()
        if color in {"sRGB", "HDR PQ", "HDR"}:
            return {"sRGB": None, "HDR PQ": "pq", "HDR": "hlg"}[color]
        raise RuntimeError(f"Unknown VIDEO color space {color!r}; select hdr_source explicitly.")
    try:
        src = video.get_stream_source()
    except Exception as exc:
        raise RuntimeError("Cannot detect VIDEO transfer; select hdr_source explicitly.") from exc
    try:
        position = src.tell() if hasattr(src, "tell") else None
    except (OSError, ValueError):
        position = None
    try:
        import av
        if hasattr(src, "seek"):
            src.seek(0)
        with av.open(src) as container:
            stream = container.streams.video[0]
            trc = getattr(stream.codec_context, "color_trc", None)
            if trc is None:
                trc = getattr(stream, "color_trc", None)
            if trc is None:
                return None
            name = getattr(trc, "name", None)
            key = str(name).lower().replace("_", "-") if name else int(trc)
            return TRC_TO_TRANSFER.get(key)
    except Exception as exc:
        raise RuntimeError("Cannot read VIDEO transfer tags; select hdr_source explicitly.") from exc
    finally:
        try:
            if position is not None and hasattr(src, "seek"):
                src.seek(position)
        except Exception:
            pass


def resolve_transfer(hdr_source, video):
    """The tool's --nr-transfer for this job: None for SDR."""
    if hdr_source not in HDR_SOURCES:
        raise ValueError("hdr_source must be auto, sdr, pq or hlg")
    if hdr_source == "auto":
        return probe_transfer(video) if video is not None else None
    return None if hdr_source == "sdr" else hdr_source


def out_dims(in_w, in_h, width, scale, height=0):
    """Output size the way nr_video.py computes it: width and height together pin the exact size,
    one of them keeps the aspect, else scale; even dims."""
    if (in_w <= 0 or in_h <= 0 or not math.isfinite(scale) or scale <= 0
            or width < 0 or height < 0 or not math.isfinite(width) or not math.isfinite(height)
            or int(width) != width or int(height) != height):
        raise ValueError("Input dimensions and scale must be positive; width/height must be nonnegative integers.")
    if width > 0 and height > 0:
        out_w, out_h = int(width), int(height)
    elif width > 0:
        out_w, out_h = int(width), round(in_h * width / in_w)
    elif height > 0:
        out_w, out_h = round(in_w * height / in_h), int(height)
    else:
        out_w, out_h = round(in_w * scale), round(in_h * scale)
    return max(2, out_w - out_w % 2), max(2, out_h - out_h % 2)


# ----------------------------------------------------------------------------- numpy cores

def check_comfy_cancelled():
    try:
        from comfy.model_management import throw_exception_if_processing_interrupted
    except ImportError:
        return
    throw_exception_if_processing_interrupted()


def cancellation_settings():
    try:
        from comfy import model_management as mm
    except ImportError:
        return {}

    def cancel_error():
        # Consume ComfyUI's interruption flag on the execution thread, never
        # from the monitor; another worker may have already consumed the flag.
        mm.throw_exception_if_processing_interrupted()
        return mm.InterruptProcessingException()

    return {"cancel_check": mm.processing_interrupted, "cancel_error": cancel_error}


def validate_frames(frames, ndim, transfer=None):
    if (not isinstance(frames, np.ndarray) or frames.ndim != ndim or frames.shape[-1] != 3
            or any(size <= 0 for size in frames.shape[:-1])
            or frames.dtype not in (np.dtype("uint8"), np.dtype("uint16"))):
        raise ValueError("Frames must be nonempty RGB arrays with uint8 (SDR) or uint16 (HDR) pixels.")
    if transfer not in (None, "pq", "hlg") or (frames.dtype == np.uint16) != bool(transfer):
        raise ValueError("HDR frames must be uint16 with pq/hlg transfer; SDR frames uint8 without a transfer.")

def run_image_np(img_u8, width, scale, nr, exe, adapter=0, height=0):
    """One uint8 RGB [H,W,3] image through --nr-run; returns uint8 RGB [H',W',3]."""
    from PIL import Image
    validate_frames(img_u8, 3)
    out_dims(img_u8.shape[1], img_u8.shape[0], width, scale, height)
    check_comfy_cancelled()
    tmp = tempfile.mkdtemp(prefix="v2d_")
    try:
        src = os.path.join(tmp, "in.png")
        out_dir = os.path.join(tmp, "out")
        Image.fromarray(np.ascontiguousarray(img_u8), "RGB").save(src)
        cmd = [exe, "--nr-run", "--in", src, "--out", out_dir, "--adapter", str(adapter)] + nr
        if width > 0:
            cmd += ["--nr-width", str(int(width))]
        if height > 0:
            cmd += ["--nr-height", str(int(height))]
        if width <= 0 and height <= 0 and abs(scale - 1.0) > 1e-6:
            cmd += ["--nr-scale", f"{scale}"]
        p = RuntimeProcess(exe, cmd[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace", **cancellation_settings())
        try:
            stdout, stderr = p.collect_output()
            p.check_timeout()
            hits = glob.glob(os.path.join(out_dir, "*_nr.png"))
            if p.returncode != 0 or not hits:
                raise RuntimeError(f"video2dlssnr failed (exit {p.returncode})\n"
                                   + ((stdout or "") + (stderr or ""))[-3000:])
        finally:
            p.close_worker()
        with Image.open(hits[0]) as image:
            return np.array(image.convert("RGB"), dtype=np.uint8)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def failed_stdout_preview(data):
    """Bound failed-worker output; Wine may put a crash report on the frame pipe."""
    if not data:
        return ""
    limit = 16384
    sample = bytes(data[:12288])
    if len(data) > limit:
        sample += b"\n[... stdout preview truncated ...]\n" + bytes(data[-4096:])
    else:
        sample = bytes(data)
    printable = sum(byte in (9, 10, 13) or 32 <= byte < 127 for byte in sample)
    if printable / len(sample) >= 0.85:
        text = sample.decode("utf-8", "replace")
        # Keep terminal control bytes inert while retaining readable crash text.
        text = "".join(char if char in "\n\t" or char.isprintable()
                       else f"\\x{ord(char):02x}" for char in text)
    else:
        text = "Non-text output; first 64 bytes (hex): " + bytes(data[:64]).hex(" ")
    return "Failed worker stdout preview (diagnostic only, not image data):\n" + text


def _batch_quote(value):
    # cmd batch expansion differs from CreateProcess argument quoting. Quotes,
    # newlines and NUL cannot safely occur in a Windows argument here. Delayed
    # expansion is disabled, and %% escapes a literal % in a batch file.
    if any(char in value for char in ('"', '\r', '\n', '\0')):
        raise RuntimeError("Wine file transport cannot quote a path/argument containing quotes or newlines.")
    return '"' + value.replace('%', '%%') + '"'


def run_video_files(cmd, frames, out_w, out_h, dtype, progress=None, log=None, sink=None):
    """Windows cmd redirects raw frames to private files, isolating Wine logs."""
    env, _ = worker_settings()
    prefix = Path(env["WINEPREFIX"]).expanduser()
    b, h, w, _ = frames.shape
    need = b * out_w * out_h * 4 * frames.dtype.itemsize
    with tempfile.TemporaryDirectory(prefix="v2d_video_") as temp:
        root = Path(temp)
        input_path, output_path, stderr_path = (root / leaf for leaf in
                                               ("input.rgba", "output.rgba", "worker.stderr"))
        if shutil.disk_usage(root).free < b * h * w * 4 * frames.dtype.itemsize + need:
            raise RuntimeError("Wine file transport needs free temporary disk space for input and output frames.")
        with input_path.open("wb") as stream:
            scratch = np.empty((h, w, 4), dtype=dtype)
            scratch[..., 3] = 65535 if dtype == np.uint16 else 255
            for frame in frames:
                check_comfy_cancelled()
                scratch[..., :3] = frame
                stream.write(scratch.tobytes())
            del scratch
        win_args = [windows_path(str(cmd[0]), prefix), *cmd[1:]]
        # nr-video has no host path flags; raw files are redirected by cmd.exe.
        redirect = (" <" + _batch_quote(windows_path(str(input_path), prefix)) +
                    " >" + _batch_quote(windows_path(str(output_path), prefix)) +
                    " 2>" + _batch_quote(windows_path(str(stderr_path), prefix)))
        batch = root / "run.cmd"
        batch.write_text("@echo off\r\nchcp 65001 >nul\r\nsetlocal DisableDelayedExpansion\r\n" +
                         " ".join(_batch_quote(value) for value in win_args) + redirect +
                         "\r\nexit /b %errorlevel%\r\n", encoding="utf-8")
        batch_win = windows_path(str(batch), prefix)
        # The batch pathname is the outer cmd /c command, before batch escaping.
        if any(char in batch_win for char in ('"', '%', '!', '\r', '\n')):
            raise RuntimeError("Wine file transport requires a temporary directory without quotes, %, ! or newlines; set TMPDIR=/tmp.")
        # Wine quotes Linux argv when creating the Windows command line.
        # Manually embedding quotes becomes literal \"...\" in Wine cmd.exe.
        p = RuntimeProcess(cmd[0], ["/d", "/c", batch_win],
                           program="cmd.exe", stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, **cancellation_settings())
        diagnostics = ""
        try:
            stdout, stderr = p.collect_output(limit=16384)
            # Wine/Unix logs are outside the raw frame file. Preserve bounded
            # diagnostics even if the launcher merges its own stdout/stderr.
            diagnostics = ((stdout or b"") + (stderr or b""))[-16384:].decode("utf-8", "replace")
            if stderr_path.is_file():
                with stderr_path.open("rb") as stream:
                    size = stderr_path.stat().st_size
                    stream.seek(max(0, size - 65536))
                    diagnostics += stream.read().decode("utf-8", "replace")
            if log is not None:
                log.append("Wine video transport: private raw frame files")
                log.extend(diagnostics.splitlines())
            p.check_timeout()
            got = output_path.stat().st_size if output_path.is_file() else 0
            if p.returncode != 0 or got != need:
                raise RuntimeError(f"video2dlssnr file transport failed (exit {p.returncode}, "
                                   f"got {got}/{need} bytes)\n" + diagnostics[-16384:])
            if progress:
                try:
                    progress(b)
                except Exception as exc:
                    if log is not None:
                        log.append(f"Progress callback failed: {exc}")
            result = np.empty((b, out_h, out_w, 3), dtype=dtype) if sink is None else None
            with output_path.open("rb") as stream:
                for index in range(b):
                    check_comfy_cancelled()
                    data = np.fromfile(stream, dtype=dtype, count=out_h * out_w * 4)
                    if data.size != out_h * out_w * 4:
                        raise RuntimeError("Worker output was truncated while reading frames.")
                    rgb = data.reshape(out_h, out_w, 4)[..., :3]
                    if sink is None:
                        result[index] = rgb
                    else:
                        sink(rgb)
            return result
        finally:
            p.close_worker()


def run_video_np(frames, width, scale, nr, motion, engine, motion_vis, exe, adapter=0,
                 progress=None, log=None, height=0, transfer=None, sdr_white=203.0, sink=None):
    """RGB [B,H,W,3] frames streamed through --nr-video; returns RGB [B,H',W',3] of the same dtype.

    uint8 frames are SDR (sRGB) and travel 8-bit. uint16 frames carry PQ / HLG code values and
    travel 16-bit with --nr-transfer, so the tool takes its HDR path. `progress(n)` is called with
    the frame count as the tool reports it; `log`, if a list, receives the tool's non-progress
    stderr lines (backend in use, final tally, any warnings).
    """
    from .stream_video import FrameSource
    if not isinstance(frames, FrameSource):
        validate_frames(frames, 4, transfer)
    check_comfy_cancelled()
    b, h, w, _ = frames.shape
    wide = frames.dtype == np.uint16
    if engine not in ENGINES or not math.isfinite(sdr_white) or sdr_white <= 0:
        raise ValueError("Invalid motion engine or SDR white luminance.")
    out_w, out_h = out_dims(w, h, width, scale, height)
    cmd = [exe, "--nr-video", "--nr-in", f"{w}x{h}", "--adapter", str(adapter)] + nr + [
        "--nr-motion", "1" if motion else "0", "--nr-motion-engine", engine]
    if transfer:
        cmd += ["--nr-transfer", transfer, "--nr-sdr-white", f"{sdr_white:g}"]
    if motion_vis:
        cmd.append("--nr-motion-vis")
    if (out_w, out_h) != (w, h):  # exact even dims so the tool and this reader agree
        cmd += ["--nr-width", str(out_w), "--nr-height", str(out_h)]

    bpp = 8 if wide else 4

    if sys.platform == "linux":
        env, _ = worker_settings()
        transport = env.get("VIDEO2DLSSNR_VIDEO_TRANSPORT", "pipe")
        if transport not in {"pipe", "files"}:
            raise ValueError("VIDEO2DLSSNR_VIDEO_TRANSPORT must be pipe or files.")
        if transport == "files":
            return run_video_files(cmd, frames, out_w, out_h, frames.dtype, progress, log, sink=sink)
    need = b * out_w * out_h * bpp
    frame_bytes = out_w * out_h * bpp
    buf = bytearray(need if sink is None else frame_bytes)
    view = memoryview(buf)
    got = 0
    p = RuntimeProcess(exe, cmd[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, **cancellation_settings())
    err = deque(maxlen=100)
    feed_errors = []

    def feed():
        try:
            scratch = np.empty((h, w, 4), dtype=frames.dtype)
            scratch[..., 3] = 65535 if wide else 255
            for frame in frames:
                check_comfy_cancelled()
                scratch[..., :3] = frame
                p.stdin.write(scratch.tobytes())
            p.stdin.close()
        except BaseException as exc:
            feed_errors.append(exc)
            p.abort()

    def drain():
        prog = re.compile(rb"^NRPROG (\d+) ")
        for raw in iter(lambda: p.stderr.readline(8192), b""):
            m = prog.match(raw)
            if m:
                if progress:
                    try:
                        progress(int(m.group(1)))
                    except Exception as exc:
                        err.append(f"Progress callback failed: {exc}")
            else:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    err.append(line[-2000:])

    tf = threading.Thread(target=feed, daemon=True)
    td = threading.Thread(target=drain, daemon=True)
    tf.start()
    td.start()

    try:
        while got < need:
            start = got if sink is None else got % frame_bytes
            n = p.stdout.readinto(view[start:])
            if not n:
                break
            got += n
            if sink is not None and got % frame_bytes == 0:
                check_comfy_cancelled()
                sink(np.frombuffer(buf, frames.dtype).reshape(out_h, out_w, 4)[..., :3])
        extra = p.stdout.read(1) if got == need else b""
        if extra:
            p.abort()
        rc = p.wait()
        tf.join(timeout=5)
        td.join(timeout=5)
        failed = p.timed_out.is_set() or bool(extra) or rc != 0 or got != need
        preview = failed_stdout_preview(view[:got]) if failed else ""
        if log is not None:
            log.extend(err)
            if preview:
                log.append(preview)
        diagnostics = "\n".join(err)[-3000:]
        if preview:
            diagnostics += "\n" + preview
        p.check_timeout()
        if feed_errors:
            raise RuntimeError("DLSS input frame stream failed") from feed_errors[0]
        if extra:
            raise RuntimeError("Unexpected extra stdout bytes: binary frame stream was "
                               "polluted or the worker returned extra frames.\n" + diagnostics)
        if rc != 0 or got != need:
            raise RuntimeError(f"video2dlssnr failed (exit {rc}, got {got}/{need} bytes)\n"
                               + diagnostics)
        return (np.frombuffer(buf, frames.dtype).reshape(b, out_h, out_w, 4)[..., :3]
                if sink is None else None)
    finally:
        if p.poll() is None:
            p.abort()
        tf.join(timeout=5)
        td.join(timeout=5)
        p.close_worker()


# ----------------------------------------------------------------------------- tensor glue

def to_u8(t):
    """ComfyUI IMAGE [B,H,W,C] float 0..1 -> uint8 RGB [B,H,W,3]."""
    validate_tensor(t)
    x = t.detach().cpu().clamp(0, 1).mul(255).round().to(torch.uint8).numpy()
    return np.ascontiguousarray(x[..., :3])


def to_u16(t):
    """ComfyUI IMAGE [B,H,W,C] float 0..1 (PQ / HLG code values) -> uint16 RGB [B,H,W,3]."""
    validate_tensor(t)
    x = t.detach().cpu().clamp(0, 1).mul(65535).round().to(torch.int32).numpy().astype(np.uint16)
    return np.ascontiguousarray(x[..., :3])


def to_tensor(arr):
    full = 65535.0 if arr.dtype == np.uint16 else 255.0
    return torch.from_numpy(np.ascontiguousarray(arr).astype(np.float32) / full)


def validate_tensor(t):
    if not isinstance(t, torch.Tensor) or t.ndim != 4 or t.shape[-1] < 3 or any(size <= 0 for size in t.shape):
        raise ValueError("IMAGE input must contain nonempty [batch, height, width, RGB] frames.")


def make_video(images, audio, frame_rate, transfer=None):
    """Wrap frames into ComfyUI's VIDEO type the way the core Create Video node does.

    An HDR clip is tagged 10-bit "HDR PQ" / "HDR" (HLG) so Save Video writes BT.2020 / PQ or HLG
    at 10 bits; the frames already hold the code values. Returns None on ComfyUI builds that
    predate the VIDEO type, so the IMAGE output still works.
    """
    try:
        from comfy_api.input_impl import VideoFromComponents
        from comfy_api.util import VideoComponents
    except ImportError as exc:
        if transfer:
            raise RuntimeError("HDR VIDEO output requires a current ComfyUI with HDR support.") from exc
        return None
    comps = VideoComponents(images=images, audio=audio, frame_rate=frame_rate)
    if transfer:
        try:
            return VideoFromComponents(comps, bit_depth=10, color_space=COMFY_COLOR_SPACE[transfer])
        except TypeError as exc:
            raise RuntimeError("This ComfyUI version cannot preserve HDR VIDEO metadata; update ComfyUI.") from exc
    return VideoFromComponents(comps)


def _nr_inputs():
    f = lambda d, lo, hi: ("FLOAT", {"default": d, "min": lo, "max": hi, "step": 0.05})
    return {
        "style": (list(STYLES), {"default": "Cinematic"}),
        "preset": (list(PRESETS), {"default": "Default"}),
        "intensity": f(1.0, 0.0, 2.0),
        "local_structure": f(1.0, 0.0, 2.0),
        "local_tone": f(1.0, 0.0, 2.0),
        "skin": f(-1.0, -1.0, 2.0),          # -1 = model default
        "detail": f(1.0, 0.0, 2.0),          # composite: 0 = original, 1 = full NR
        "color": f(1.0, 0.0, 1.0),           # 0 = keep original hue, 1 = NR colour
        "ui_correction": ("BOOLEAN", {"default": False}),
        "auto_mask": ("BOOLEAN", {"default": False}),
        "scale": ("FLOAT", {"default": 1.0, "min": 1.0, "max": 3.0, "step": 0.05}),
        "width": ("INT", {"default": 0, "min": 0, "max": 7680, "step": 2,
                          "tooltip": "Output width (height by aspect unless height is set too). "
                                     "0 = use height, else scale."}),
        "height": ("INT", {"default": 0, "min": 0, "max": 7680, "step": 2,
                           "tooltip": "Output height (width by aspect unless width is set too). "
                                      "0 = use width, else scale."}),
        "adapter": ("INT", {"default": 0, "min": 0, "max": 15,
                            "tooltip": "DXGI adapter index (which GPU)."}),
        "sr_preset": (SR_PRESETS, {"default": "Default",
                                   "tooltip": "DLSS Super Resolution model preset for the upscale. "
                                              "Default = driver picks per mode; E = CNN (DLSS 3.7 "
                                              "default), F = CNN (Ultra Performance / DLAA), J = "
                                              "DLSS 4 transformer (first), K = DLSS 4 transformer "
                                              "(default), L / M = DLSS 4.5 transformer (newest)."}),
    }


# ----------------------------------------------------------------------------- nodes

class DLSSNRImage:
    """Each image in the batch through DLSS SR + Neural Rendering independently."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), **_nr_inputs()}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"
    CATEGORY = "video2dlssnr"
    DESCRIPTION = ("DLSS Super Resolution + Neural Rendering on each image independently "
                   "(same as the Image tab). width pins the output width, else scale.")

    def run(self, image, style, preset, intensity, local_structure, local_tone, skin,
            detail, color, ui_correction, auto_mask, scale, width, height, adapter, sr_preset):
        exe = find_exe()
        nr = nr_args(style, preset, intensity, local_structure, local_tone, skin,
                     detail, color, ui_correction, auto_mask, sr_preset)
        src = to_u8(image)
        outs = [run_image_np(src[i], width, scale, nr, exe, adapter, height)
                for i in range(src.shape[0])]
        return (to_tensor(np.stack(outs)),)


class DLSSNRVideo:
    """A clip streamed through --nr-video with motion vectors for temporal NR.

    Takes either the core Load Video output (VIDEO — audio and frame rate carried over) or a plain
    IMAGE batch (e.g. VideoHelperSuite), and returns both the frames and a ready VIDEO.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                **_nr_inputs(),
                "motion": ("BOOLEAN", {"default": True,
                                       "tooltip": "Optical-flow motion vectors for temporal stability."}),
                "motion_engine": (ENGINES, {"default": "auto"}),
                "motion_vis": ("BOOLEAN", {"default": False,
                                           "tooltip": "Debug: output the flow field instead of NR."}),
                "hdr_source": (HDR_SOURCES, {"default": "auto",
                                             "tooltip": "auto: read the connected VIDEO's transfer "
                                                        "tag (PQ / HLG clips take the HDR path; an IMAGE "
                                                        "batch counts as SDR). sdr / pq / hlg override "
                                                        "it for a mistagged clip. HDR output: feed the "
                                                        "VIDEO to Save Video (10-bit, HDR PQ / HDR)."}),
                "sdr_white": ("FLOAT", {"default": 203.0, "min": 50.0, "max": 1000.0, "step": 1.0,
                                        "tooltip": "HDR PQ: the luminance in nits the model is shown as "
                                                   "SDR white (203 = BT.2408 graphics white; lower = "
                                                   "brighter proxy, more detail in the shadows)."}),
            },
            "optional": {
                "video": ("VIDEO", {"tooltip": "From the core Load Video node. Its audio and frame "
                                               "rate are carried over to the video output."}),
                "images": ("IMAGE", {"tooltip": "Frame batch (e.g. VideoHelperSuite Load Video). "
                                                "Used when no video is connected."}),
                "images_fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                                         "tooltip": "Only with an IMAGE input: frame-rate metadata for "
                                                    "the VIDEO output (the batch carries none). Does NOT "
                                                    "add frames — the frame count stays the same. Ignored "
                                                    "when a VIDEO is connected."}),
                "output_mode": (["auto", "file_video", "images_and_video"], {"default": "auto",
                    "tooltip": "auto: VIDEO-only graphs stream to a file; connected IMAGE output keeps legacy tensors. file_video blocks IMAGE output to keep RAM bounded."}),
            },
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, output_mode="auto", prompt=None, unique_id=None, **kwargs):
        # Graph consumers are not ordinary input values in Comfy's cache key.
        # Reconnecting IMAGE after a VIDEO-only run must not reuse a blocker.
        from .stream_video import wants_file_video
        return wants_file_video(output_mode, prompt, unique_id)

    RETURN_TYPES = ("IMAGE", "VIDEO")
    RETURN_NAMES = ("images", "video")
    FUNCTION = "run"
    CATEGORY = "video2dlssnr"
    DESCRIPTION = ("A clip through DLSS SR + Neural Rendering with optical-flow motion vectors for "
                   "temporal stability (same as the Video tab). Connect the core Load Video (VIDEO) "
                   "or an IMAGE batch; get frames and a ready VIDEO (audio and fps kept) back. HDR "
                   "clips (PQ / HLG) are handled as HDR and the VIDEO output is tagged accordingly.")

    def run(self, style, preset, intensity, local_structure, local_tone, skin,
            detail, color, ui_correction, auto_mask, scale, width, height, adapter, sr_preset,
            motion, motion_engine, motion_vis, hdr_source="auto", sdr_white=203.0, video=None,
            images=None, images_fps=24.0, output_mode="auto", prompt=None, unique_id=None):
        from .stream_video import run_streaming_video
        return run_streaming_video(self, locals())

    def _legacy_run(self, style, preset, intensity, local_structure, local_tone, skin,
            detail, color, ui_correction, auto_mask, scale, width, height, adapter, sr_preset,
            motion, motion_engine, motion_vis, hdr_source="auto", sdr_white=203.0, video=None,
            images=None, images_fps=24.0):
        audio, frame_rate = None, None
        transfer = resolve_transfer(hdr_source, video)
        if video is not None:
            comps = video.get_components()
            images, audio, frame_rate = comps.images, comps.audio, comps.frame_rate
        if images is None:
            raise RuntimeError("Connect a VIDEO (Load Video) or an IMAGE batch (e.g. VHS Load Video).")
        if frame_rate is None:  # an IMAGE batch carries no rate; this only labels the VIDEO output
            frame_rate = Fraction(images_fps).limit_denominator(1000)
        if not math.isfinite(float(frame_rate)) or frame_rate <= 0:
            raise ValueError("Video frame rate must be positive and finite.")
        validate_tensor(images)
        # Check output API support before running a costly GPU job.
        if make_video(images, audio, frame_rate, transfer) is None and video is not None:
            raise RuntimeError("VIDEO input/output requires a current ComfyUI version.")

        exe = find_exe()
        nr = nr_args(style, preset, intensity, local_structure, local_tone, skin,
                     detail, color, ui_correction, auto_mask, sr_preset)
        # HDR frames keep their PQ / HLG code values and travel 16-bit; SDR frames travel 8-bit.
        src = to_u16(images) if transfer else to_u8(images)
        total = src.shape[0]
        pbar = None
        try:
            from comfy.utils import ProgressBar  # only exists inside ComfyUI
            pbar = ProgressBar(total)
        except Exception:
            pass

        def progress(done):
            if pbar is not None:
                pbar.update_absolute(min(done, total))

        out = run_video_np(src, width, scale, nr, motion, motion_engine, motion_vis, exe, adapter,
                           progress, height=height, transfer=transfer, sdr_white=sdr_white)
        out_t = to_tensor(out)
        return (out_t, make_video(out_t, audio, frame_rate, transfer))


class DLSSNRRuntimeInfo:
    """Diagnostics: pushes two small frames through the real pipeline and reports the outcome."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"adapter": ("INT", {"default": 0, "min": 0, "max": 15})}}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("info",)
    FUNCTION = "info"
    CATEGORY = "video2dlssnr"
    OUTPUT_NODE = True
    DESCRIPTION = ("Runs a tiny test job through the tool and reports whether Neural Rendering "
                   "works on this machine, plus the optical-flow backend in use.")

    def info(self, adapter):
        lines = []
        try:
            exe = find_exe()
        except RuntimeError as e:
            return (str(e),)
        lines.append(f"exe: {exe}")
        try:
            command, options, timeout = launch_spec(exe, ["--help"])
            lines.append(f"launcher: {command[0]}")
            lines.append(f"worker timeout: {timeout:g}s")
            if options.get("start_new_session"):
                lines.append(f"Wine prefix: {options['env']['WINEPREFIX']}")
        except RuntimeError as exc:
            return ("\n".join(lines + ["runtime configuration: FAILED", str(exc)]),)
        try:
            if torch.cuda.is_available():
                lines.append(f"cuda device: {torch.cuda.get_device_name(0)}")
        except Exception:
            pass

        # A real 2-frame job at 1280x720, native res (NR only), motion vectors on.
        h, w = 720, 1280
        yy, xx = np.mgrid[0:h, 0:w]
        f = np.stack([xx * 255 // w, yy * 255 // h, (xx + yy) % 256], -1).astype(np.uint8)
        frames = np.stack([f, f])
        nr = nr_args("Default", "Default", 1.0, 1.0, 1.0, -1.0,
                     detail=1.0, color=1.0, ui_correction=False, auto_mask=False)
        log = []
        t0 = time.time()
        try:
            out = run_video_np(frames, 0, 1.0, nr, True, "auto", False, exe, adapter, log=log)
            lines.append(f"neural rendering: OK  ({w}x{h}, 2 frames, {time.time() - t0:.1f}s, "
                         f"output {out.shape[2]}x{out.shape[1]})")
        except RuntimeError as e:
            lines.append("neural rendering: FAILED")
            lines.append(str(e))
        lines.extend(log)
        return ("\n".join(lines)[-4000:],)


NODE_CLASS_MAPPINGS = {
    "DLSSNR_Image": DLSSNRImage,
    "DLSSNR_Video": DLSSNRVideo,
    "DLSSNR_RuntimeInfo": DLSSNRRuntimeInfo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DLSSNR_Image": "DLSS Neural Rendering (Image)",
    "DLSSNR_Video": "DLSS Neural Rendering (Video)",
    "DLSSNR_RuntimeInfo": "DLSS NR Runtime Check",
}
