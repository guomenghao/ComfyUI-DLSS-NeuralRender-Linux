"""Build and test an optional FFmpeg-only NVENC enumeration workaround.

Does not install drivers, change device nodes, edit startup files, or set global
LD_PRELOAD. nvenc_fix.c is a separate GPL-3.0 component; see its license file.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def wrapper_text(library, ffmpeg):
    if any(c.isspace() or c == ":" for c in str(library)):
        raise ValueError("LD_PRELOAD library path cannot contain whitespace or ':'")
    return ("#!/bin/sh\nset -eu\n" +
            "export LD_PRELOAD=" + shlex.quote(str(library)) +
            '"${LD_PRELOAD:+:$LD_PRELOAD}"\nexec ' +
            shlex.quote(str(ffmpeg)) + ' "$@"\n')


def build():
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise RuntimeError("This experimental helper requires Linux x86_64")
    gcc = shutil.which("gcc")
    if not gcc:
        raise RuntimeError("Install gcc and libc6-dev, then retry")
    ffmpeg, ffprobe = Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")
    if not all(p.is_file() and os.access(p, os.X_OK) for p in (ffmpeg, ffprobe)):
        raise RuntimeError("Missing /usr/bin/ffmpeg or /usr/bin/ffprobe")
    data = (ROOT / "nvenc_fix.c").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    base = Path(os.environ.get("V2D_NVENC_STATE",
                               str(Path.home() / ".local/share/comfyui-nvenc-fix"))).resolve()
    destination = base / digest
    # Validate the path before compiling or writing a wrapper.
    wrapper_text(destination / "libnvenc_fix.so", ffmpeg)
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=base) as folder:
        stage = Path(folder)
        (stage / "nvenc_fix.c").write_bytes(data)
        subprocess.run([gcc, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-o", str(stage / "libnvenc_fix.so"),
                        str(stage / "nvenc_fix.c"), "-ldl"], check=True)
        library = stage / "libnvenc_fix.so"
        elf = library.read_bytes()[:20]
        if elf[:6] != b"\x7fELF\x02\x01" or int.from_bytes(elf[18:20], "little") != 62:
            raise RuntimeError("Compiler did not produce x86_64 Linux ELF")
        (stage / "ffmpeg").write_text(wrapper_text(destination / "libnvenc_fix.so", ffmpeg))
        (stage / "ffmpeg").chmod(0o755)
        shutil.copy2(ROOT / "NVENC_FIX_LICENSE.txt", stage / "NVENC_FIX_LICENSE.txt")
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise RuntimeError("Build destination is not a regular directory")
            for name in ("nvenc_fix.c", "ffmpeg", "NVENC_FIX_LICENSE.txt", "libnvenc_fix.so"):
                file = destination / name
                if file.is_symlink() or not file.is_file():
                    raise RuntimeError("Existing build is incomplete; use a new V2D_NVENC_STATE")
            if (destination / "nvenc_fix.c").read_bytes() != data:
                raise RuntimeError("Existing source does not match")
            # Preserve past results; replace only the verified build products.
            for name in ("libnvenc_fix.so", "ffmpeg"):
                os.replace(stage / name, destination / name)
        else:
            os.rename(stage, destination)
    return destination, ffprobe, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("test", "path"), nargs="?", default="test")
    args = parser.parse_args()
    destination, ffprobe, digest = build()
    wrapper = destination / "ffmpeg"
    if args.mode == "path":
        print(wrapper)
        return
    result_dir = Path(tempfile.mkdtemp(prefix="gpu-test-", dir=destination))
    print("FFmpeg-only wrapper:", wrapper, flush=True)
    print("Test logs:", result_dir, flush=True)
    print("Experimental workaround: success requires actual GPU output.", flush=True)
    env = os.environ.copy()
    env["NVENC_FIX_DEBUG"] = "1"
    results = {"status": "FAIL", "source_sha256": digest, "wrapper": str(wrapper),
               "audio": "synthetic test has no audio", "tests": []}
    try:
        for size in ("256x256", "1296x720"):
            output = result_dir / f"h264-{size}.mp4"
            command = [str(wrapper), "-hide_banner", "-v", "verbose",
                       "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                       "-frames:v", "12", "-c:v", "h264_nvenc", "-gpu", "0",
                       "-pix_fmt", "yuv420p", str(output)]
            log = result_dir / f"h264-{size}.log"
            with log.open("wb") as stream:
                test = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                      stdout=stream, stderr=subprocess.STDOUT, timeout=60)
            record = {"size": size, "exit_code": test.returncode, "log": str(log)}
            results["tests"].append(record)
            print(log.read_text(errors="replace")[-14000:], flush=True)
            if test.returncode:
                raise RuntimeError(f"NVENC initialization/encoding failed; see {log}")
            probe = subprocess.run([str(ffprobe), "-v", "error", "-count_frames",
                                    "-select_streams", "v:0", "-show_entries",
                                    "stream=codec_name,width,height,nb_read_frames",
                                    "-of", "json", str(output)], capture_output=True,
                                   text=True, check=True, timeout=20)
            info = json.loads(probe.stdout)["streams"][0]
            width, height = map(int, size.split("x"))
            if (info["codec_name"] != "h264" or info["width"] != width or
                    info["height"] != height or int(info["nb_read_frames"]) != 12):
                raise RuntimeError(f"Unexpected encoded video: {info}")
            # Decode every frame; metadata alone does not establish valid video.
            decode = subprocess.run(["/usr/bin/ffmpeg", "-v", "error", "-i", str(output),
                                     "-map", "0:v:0", "-f", "null", "-"],
                                    capture_output=True, text=True, timeout=30)
            if decode.returncode:
                raise RuntimeError("Encoded video decode failed: " + decode.stderr[-2000:])
            record["decoded"] = info
        results["status"] = "PASS"
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, RuntimeError) as exc:
        results["error"] = str(exc)
    (result_dir / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    if results["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        print("FAILED:", exc, file=sys.stderr)
        raise SystemExit(1)
