"""Deploy this patch into an existing verified v1.4.1 node and its own prefix."""
import argparse
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from pathlib import Path

ORIGINAL_HASHES = {
    "video2dlssnr.exe": "7658f780aaa0ce9112d93a036273e19c4d62bf03ffae1a7bb6099d485a97ff58",
    "nvngx_dlss.dll": "c85f971ce023c9f3492fc7455f0b01a24ba18ea39636407a846902c4360b0b7e",
    "nvngx_dlssnr.dll": "8270b350cd82de5ce89806872cdd6b6a9249b80836b91bbeb3573470744cc206",
    "nvngx.dll_dlssnr.dll": "a64d48ec6157a6811f9b812de47ecb718f4143c3355879c22515e6418b7c1bea",
}
GRAPHICS = {
    "dxgi.dll": "lib/wine/dxvk/x86_64-windows/dxgi.dll",
    "d3d11.dll": "lib/wine/dxvk/x86_64-windows/d3d11.dll",
    "d3d12.dll": "lib/wine/vkd3d-proton/x86_64-windows/d3d12.dll",
    "d3d12core.dll": "lib/wine/vkd3d-proton/x86_64-windows/d3d12core.dll",
    "nvapi64.dll": "lib/wine/nvapi/x86_64-windows/nvapi64.dll",
    "nvofapi64.dll": "lib/wine/nvapi/x86_64-windows/nvofapi64.dll",
}
MARKER = "video2dlssnr 610.57.04 dedicated prefix v1\n"


def pe64(path):
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                return False
            stream.seek(struct.unpack_from("<I", header, 60)[0])
            return stream.read(6) == b"PE\0\0\x64\x86"
    except OSError:
        return False


def check_node(node):
    for name, digest in ORIGINAL_HASHES.items():
        path = node / "bin" / name
        if not pe64(path):
            raise RuntimeError(f"Missing/invalid x64 runtime: {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Runtime differs from verified v1.4.1: {path}; inspect version before deploying.")
    print("Existing node has the four verified v1.4.1 runtime binaries.", flush=True)


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)  # Replace a destination symlink; never follow it.
    finally:
        temporary.unlink(missing_ok=True)


def validate_prefix(prefix):
    if prefix.is_symlink():
        raise RuntimeError(f"Refusing a symlink prefix: {prefix}")
    marker = prefix / ".video2dlssnr-owner"
    if prefix.exists() and (not marker.is_file() or marker.read_text() != MARKER):
        raise RuntimeError(f"Refusing existing unmanaged prefix: {prefix}. Choose a new V2D_STATE directory.")


def run(command, env, timeout=180):
    with subprocess.Popen(command, env=env, start_new_session=True) as process:
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    if code:
        raise RuntimeError(f"Setup command failed with exit {code}: {command[0]}")


def configure(args):
    node, state, proton = args.node.resolve(), args.state.resolve(), args.proton.resolve()
    check_node(node)
    sources = {name: proton / rel for name, rel in GRAPHICS.items()}
    sources["d3dcompiler_47.dll"] = args.compiler.resolve()
    for name, path in sources.items():
        if not pe64(path):
            raise RuntimeError(f"Missing/invalid x64 {name}: {path}")
    bridges = [p for p in args.driver_dir.rglob("_nvngx.dll") if pe64(p)]
    if not bridges:
        raise RuntimeError("The extracted matching driver contains no x64 _nvngx.dll.")
    if len({hashlib.sha256(p.read_bytes()).hexdigest() for p in bridges}) != 1:
        raise RuntimeError("Multiple different x64 NGX bridges found; inspect the extracted package.")
    bridge = min(bridges, key=lambda p: len(p.parts)).resolve()
    wine, server = proton / "bin/wine", proton / "bin/wineserver"
    if not os.access(wine, os.X_OK) or not os.access(server, os.X_OK):
        raise RuntimeError("GE-Proton Wine/wineserver is missing or not executable.")
    prefix = state / "prefix"
    validate_prefix(prefix)
    if not (node / "wine_runtime.py").is_file():
        raise RuntimeError("Repository node code is missing.")
    env = os.environ.copy()
    env.update({"WINEPREFIX": str(prefix), "WINESERVER": str(server),
                "WINEARCH": "win64", "WINEDEBUG": "-all", "WINE_GRAPHICS_DRIVER": "x11"})
    # Unrelated shell overrides must not replace builtin DLLs during first init.
    env.pop("WINEDLLOVERRIDES", None)
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / ".video2dlssnr-owner").write_text(MARKER)
    system32 = prefix / "drive_c/windows/system32"
    if not (system32.is_dir() and (prefix / "system.reg").is_file()):
        run(["xvfb-run", "-a", "--server-args=-screen 0 1280x720x24 -nolisten tcp",
             str(wine), "wineboot", "-u"], env)
    if not system32.is_dir() or system32.is_symlink():
        raise RuntimeError("Wine did not create a regular system32 directory.")
    backup = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=_backup_root(state)))
    old_compiler = system32 / "d3dcompiler_47.dll"
    if old_compiler.is_file():
        atomic_copy(old_compiler, backup / "prefix-system32/d3dcompiler_47.dll")
    for name, path in sources.items():
        atomic_copy(path, system32 / name)
    # Repository code is already present; back up only machine-local files.
    for rel in [Path("linux-runtime.json"), Path("bin/_nvngx.dll")]:
        if (node / rel).exists():
            atomic_copy(node / rel, backup / rel)
    atomic_copy(bridge, node / "bin/_nvngx.dll")
    settings = {
        "VIDEO2DLSSNR_WINE": str(wine), "WINEPREFIX": str(prefix), "WINESERVER": str(server),
        "WINEDEBUG": "-all", "WINE_GRAPHICS_DRIVER": "x11", "VIDEO2DLSSNR_XVFB": "1",
        "WINEDLLOVERRIDES": "dxgi,d3d11,d3d12,d3d12core,nvapi64,nvofapi64,d3dcompiler_47=n,b",
        "DXVK_ENABLE_NVAPI": "1", "DXVK_CONFIG": "dxgi.hideNvidiaGpu = False",
        "WINE_HEAP_DELAY_FREE": "1", "WINE_HEAP_ZERO_MEMORY": "1",
        "DXVK_FILTER_DEVICE_NAME": "NVIDIA GeForce RTX 5090",
        "VKD3D_FILTER_DEVICE_NAME": "NVIDIA GeForce RTX 5090",
        "VIDEO2DLSSNR_VIDEO_TRANSPORT": "files",
    }
    cfg = node / "linux-runtime.json"
    tmp_cfg = state / "new-linux-runtime.json"
    tmp_cfg.write_text(json.dumps({"version": 1, "timeout_seconds": 600, "environment": settings}, indent=2))
    atomic_copy(tmp_cfg, cfg)
    manifest = {"driver": "610.57.04", "bridge_source": str(bridge),
        "bridge_sha256": hashlib.sha256(bridge.read_bytes()).hexdigest(),
        "prefix": str(prefix), "backup": str(backup),
        "compiler_sha256": hashlib.sha256(args.compiler.read_bytes()).hexdigest(),
        "video_transport": "files",
        "configuration_files": ["linux-runtime.json", "bin/_nvngx.dll"],
        "configuration_only": "Actual GPU tests not yet run; execute setup_ubuntu24.sh test."}
    (state / "deployment.json").write_text(json.dumps(manifest, indent=2))
    print(f"Patch backup: {backup}\nBridge: {bridge}\nSaved configuration: {cfg}", flush=True)


def _backup_root(state):
    root = state / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    check = sub.add_parser("check-node")
    check.add_argument("--node", type=Path, required=True)
    config = sub.add_parser("configure")
    for field in ["node", "state", "proton", "compiler", "driver-dir"]:
        config.add_argument("--" + field, type=Path, required=True)
    args = parser.parse_args()
    try:
        check_node(args.node) if args.mode == "check-node" else configure(args)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, "FAILED: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
