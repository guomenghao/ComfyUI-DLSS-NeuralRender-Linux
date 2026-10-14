"""Persist a previously GPU-tested NVENC wrapper for ComfyUI and CompShare boot.

Installation backs up and updates only start.sh, stop.sh, comfyui-runtime.json,
and the selected boot script. It does not restart services or install drivers.
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

from nvenc_container import wrapper_text

ROOT = Path(__file__).resolve().parent
FFMPEG = Path("/usr/bin/ffmpeg")
FFPROBE = Path("/usr/bin/ffprobe")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Expected a regular file: {path}")
    return path.read_bytes()


def validate_runtime(runtime):
    runtime = Path(runtime).resolve()
    digest = sha((ROOT / "nvenc_fix.c").read_bytes())
    if sha(regular(runtime / "nvenc_fix.c")) != digest:
        raise RuntimeError("NVENC runtime source differs from this repository version")
    library = runtime / "libnvenc_fix.so"
    library_data = regular(library)
    header = library_data[:20]
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise RuntimeError("NVENC library must be x86_64 Linux ELF")
    wrapper = runtime / "ffmpeg"
    wrapper_data = regular(wrapper)
    if wrapper_data != wrapper_text(library, FFMPEG).encode():
        raise RuntimeError("FFmpeg wrapper is not the expected scoped wrapper")
    for file in (wrapper, FFMPEG, FFPROBE):
        if not file.is_file() or not os.access(file, os.X_OK):
            raise RuntimeError(f"Missing executable: {file}")
    passed = None
    for report in sorted(runtime.glob("gpu-test-*/results.json")):
        try:
            if report.is_symlink() or report.stat().st_size > 65536:
                continue
            saved = json.loads(report.read_text())
            if (saved.get("status") != "PASS" or saved.get("source_sha256") != digest
                    or saved.get("wrapper") != str(wrapper)):
                continue
            tests = saved["tests"]
            expected = {"256x256": (256, 256), "1296x720": (1296, 720)}
            if len(tests) != 2 or {t["size"] for t in tests} != set(expected):
                continue
            for test in tests:
                info = test["decoded"]
                width, height = expected[test["size"]]
                if (test["exit_code"] != 0 or info["codec_name"] != "h264"
                        or info["width"] != width or info["height"] != height
                        or int(info["nb_read_frames"]) != 12):
                    raise ValueError("GPU result does not match")
            passed = report
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if passed is None:
        raise RuntimeError("No matching two-size GPU PASS report; run nvenc_container.py test first")
    config = {"version": 1,
              "environment": {"DLSS_FFMPEG_PATH": str(wrapper),
                              "DLSS_FFPROBE_PATH": str(FFPROBE)},
              "nvenc": {"source_sha256": digest, "library": str(library),
                        "library_sha256": sha(library_data),
                        "wrapper_sha256": sha(wrapper_data)}}
    return config, passed


def boot_text(comfy):
    return ("#!/usr/bin/env bash\n# Managed by install_nvenc_startup.py\n"
            "# One startup path for manual control and container boot.\n"
            "set -Eeuo pipefail\nexec bash " + shlex.quote(str(comfy / "start.sh")) + "\n")


def atomic_write(target, data, mode):
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".nvenc-startup-",
                                     delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def install(comfy, boot, runtime, check=False):
    comfy, boot = Path(comfy).resolve(), Path(boot).absolute()
    if not (comfy / "main.py").is_file() or not boot.parent.is_dir():
        raise RuntimeError("ComfyUI main.py or boot-script directory is missing")
    config, report = validate_runtime(runtime)
    plan = [
        (comfy / "start.sh", regular(ROOT / "start.sh"), 0o755, "start.sh"),
        (comfy / "stop.sh", regular(ROOT / "stop.sh"), 0o755, "stop.sh"),
        (comfy / "comfyui-runtime.json", (json.dumps(config, indent=2) + "\n").encode(),
         0o600, "comfyui-runtime.json"),
        (boot, boot_text(comfy).encode(), 0o755, "boot-comfyui.sh"),
    ]
    originals = {}
    changed = []
    for target, data, mode, label in plan:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError(f"Refusing symlink or non-file destination: {target}")
        original = (target.read_bytes(), target.stat().st_mode & 0o777) if target.exists() else None
        originals[target] = original
        if original != (data, mode):
            changed.append((target, data, mode, label))
        if target.suffix == ".sh":
            subprocess.run(["bash", "-n"], input=data, check=True, capture_output=True)
    print("Verified GPU PASS:", report)
    print("Persistent FFmpeg:", config["environment"]["DLSS_FFMPEG_PATH"])
    print("Boot entry:", boot)
    if check:
        print("CHECK ONLY:", len(changed), "file(s) would change")
        return None
    if not changed:
        print("Already configured; no files changed.")
        return None
    backup_root = comfy / "node_backups"
    if backup_root.is_symlink():
        raise RuntimeError("node_backups cannot be a symlink")
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="nvenc-startup-", dir=backup_root))
    manifest = []
    for target, data, mode, label in changed:
        old = originals[target]
        if old is not None:
            shutil.copy2(target, backup / label)
        manifest.append({"target": str(target), "backup": label if old else None})
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    installed = []
    try:
        for target, data, mode, label in changed:
            atomic_write(target, data, mode)
            installed.append(target)
    except BaseException:
        for target in reversed(installed):
            old = originals[target]
            if old is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, old[0], old[1])
        raise
    print("Backup:", backup)
    print("INSTALL COMPLETE. Restart ComfyUI with ./stop.sh && ./start.sh")
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfyui", type=Path, default=Path.home() / "ComfyUI")
    parser.add_argument("--boot-script", type=Path, default=Path("/start.d/comfyui.sh"))
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("Deployment requires the Linux server")
    digest = sha((ROOT / "nvenc_fix.c").read_bytes())
    runtime = args.runtime or Path(os.environ.get("V2D_NVENC_STATE",
                              str(Path.home() / ".local/share/comfyui-nvenc-fix"))) / digest
    install(args.comfyui, args.boot_script, runtime, check=args.check)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("FAILED:", exc, file=sys.stderr)
        raise SystemExit(1)
