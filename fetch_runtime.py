"""Fetch verified upstream v1.4.1 binaries, without overwriting this node's code."""
import argparse
import hashlib
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

URL = "https://github.com/DaniilSokolyuk/video2dlssnr/releases/download/v1.4.1/video2dlssnr-comfyui.zip"
ARCHIVE_SHA256 = "4eda7e13d1d6bac229309297e33f51d1210987f86249b75dd03530a86302798b"
ARCHIVE_BYTES = 247413258
FILES = {
    "ffmpeg.exe": "2e1b00eb8fe56ae789a4ca888a66e328b4301add661f8846bf90d2b3a46c1bcf",
    "ffprobe.exe": "40d4bc84fa99fb5efc2ef812ec429414cc3b484334a66971b02bb009db581252",
    "nvngx.dll_dlssnr.dll": "a64d48ec6157a6811f9b812de47ecb718f4143c3355879c22515e6418b7c1bea",
    "nvngx_dlss.dll": "c85f971ce023c9f3492fc7455f0b01a24ba18ea39636407a846902c4360b0b7e",
    "nvngx_dlssnr.dll": "8270b350cd82de5ce89806872cdd6b6a9249b80836b91bbeb3573470744cc206",
    "video2dlssnr.exe": "7658f780aaa0ce9112d93a036273e19c4d62bf03ffae1a7bb6099d485a97ff58",
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(path):
    if path.stat().st_size != ARCHIVE_BYTES or sha256(path) != ARCHIVE_SHA256:
        raise RuntimeError("Upstream archive size/SHA-256 differs from verified v1.4.1; nothing installed.")


def install_archive(archive, node):
    verify_archive(archive)
    node.mkdir(parents=True, exist_ok=True)
    # Verify every required member BEFORE modifying any destination.
    with tempfile.TemporaryDirectory(prefix="v2d-runtime-", dir=node) as temp:
        staged = Path(temp)
        with zipfile.ZipFile(archive) as z:
            entries = {}
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise RuntimeError("Unsafe path in upstream archive")
                if name in entries:
                    raise RuntimeError("Duplicate normalized path in upstream archive")
                entries[name] = info
            for leaf, digest in FILES.items():
                key = "video2dlssnr/bin/" + leaf
                if key not in entries:
                    raise RuntimeError(f"Upstream runtime missing: {leaf}")
                with z.open(entries[key]) as src, (staged / leaf).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if sha256(staged / leaf) != digest:
                    raise RuntimeError(f"Upstream binary checksum mismatch: {leaf}")
                target = node / "bin" / leaf
                if target.exists() and sha256(target) != digest:
                    raise RuntimeError(f"Refusing to overwrite a different binary: {target}")
        (node / "bin").mkdir(exist_ok=True)
        for leaf in FILES:
            os.replace(staged / leaf, node / "bin" / leaf)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--cache", type=Path, default=Path.home()/".cache/video2dlssnr")
    parser.add_argument("--archive", type=Path, help="Use a locally downloaded official archive")
    args = parser.parse_args()
    try:
        if all((args.node/"bin"/f).is_file() and sha256(args.node/"bin"/f) == h for f,h in FILES.items()):
            print("All six upstream runtime binaries are already verified.")
            return
        archive = args.archive or args.cache/"video2dlssnr-comfyui-v1.4.1.zip"
        if not args.archive:
            args.cache.mkdir(parents=True, exist_ok=True)
            try:
                verify_archive(archive)
            except (OSError, RuntimeError):
                temporary = archive.with_suffix(".part")
                request = urllib.request.Request(URL, headers={"User-Agent":"ComfyUI-DLSS-NeuralRender-Linux/0.1.0"})
                print("Downloading verified upstream runtime asset: " + URL, flush=True)
                with urllib.request.urlopen(request, timeout=120) as src, temporary.open("wb") as dst:
                    shutil.copyfileobj(src,dst)
                verify_archive(temporary)
                temporary.replace(archive)
        install_archive(archive, args.node)
        print("Six verified binaries installed; repository Python files retained.")
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        parser.exit(1,"FAILED: "+str(exc)+"\n")


if __name__ == "__main__":
    main()
