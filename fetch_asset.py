"""Resume pinned runtime downloads with curl, keeping partial files on failure."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import time


def matches(path: Path, digest: str) -> bool:
    if not path.is_file():
        return False
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == digest


def fetch(url: str, target: Path, digest: str, *, attempts: int = 4,
          retry_delay: float = 1) -> None:
    target = Path(target)
    if matches(target, digest):
        print(f"Verified cache: {target}", flush=True)
        return
    partial = Path(str(target) + ".part")
    target.parent.mkdir(parents=True, exist_ok=True)
    # A completed .part may remain after an interrupted verification/rename.
    if matches(partial, digest):
        partial.replace(target)
        print(f"Verified completed partial: {target}", flush=True)
        return
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        print(f"Download attempt {attempt + 1}/{attempts}; resume at {offset} bytes",
              flush=True)
        # Each retry starts a new curl with the NEW partial size. curl's own
        # retry can rewind to its initial offset, losing progress on long files.
        result = subprocess.run([
            "curl", "--fail", "--location", "--continue-at", "-",
            "--connect-timeout", "30", "--max-time", "0",
            "--speed-limit", "1024", "--speed-time", "120",
            "--output", str(partial), url,
        ], check=False)
        if matches(partial, digest):
            partial.replace(target)
            print(f"SHA-256 verified: {target}", flush=True)
            return
        if result.returncode == 0:
            raise RuntimeError(
                f"SHA-256 mismatch; preserved {partial}. Expected {digest}. "
                "Do not extract it. Move the partial aside before downloading afresh.")
        if result.returncode in (33, 36):
            raise RuntimeError(
                f"Download source cannot resume; preserved {partial}. "
                "Choose another source supporting HTTP Range, or move the partial "
                "aside before downloading afresh.")
        if attempt + 1 < attempts:
            time.sleep(retry_delay * (2 ** attempt))
    raise RuntimeError(f"curl failed ({result.returncode}); preserved {partial}. "
                       "Rerun to resume, or choose another download source.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("target", type=Path)
    parser.add_argument("sha256")
    args = parser.parse_args()
    try:
        fetch(args.url, args.target, args.sha256)
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"Download failed: {exc}\n")


if __name__ == "__main__":
    main()
