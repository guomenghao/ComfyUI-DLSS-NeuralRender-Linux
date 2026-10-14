#!/usr/bin/env bash
# Dedicated local Wine setup for Ubuntu 24.04 / RTX 5090 / 610.57.04.
# NVIDIA archives are EXTRACTED ONLY. Nothing installs a kernel/display driver.
set -Eeuo pipefail
kit_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
node_dir="${V2D_NODE_DIR:-$kit_dir}"
state_dir="${V2D_STATE:-$HOME/.local/share/video2dlssnr-linux-610.57.04}"
py_exec="${V2D_PYTHON:-$(command -v python)}"
mode="${1:-help}"
if [[ "$mode" == help ]]; then
    printf '%s\n' 'Usage: bash setup_ubuntu24.sh install|test' \
      'Optional: V2D_NODE_DIR, V2D_STATE, V2D_PYTHON, V2D_PROTON_URL.' \
      'install: dependencies, pinned downloads, code backup/deploy, dedicated prefix.' \
      'test: help -> production NR probe -> real image/video smoke test, stop on failure.'
    exit 0
fi
[[ "$mode" == install || "$mode" == test ]] || { printf 'Unknown mode: %s\n' "$mode"; exit 2; }
mkdir -p "$state_dir"
exec > >(tee -a "$state_dir/$mode.log") 2>&1
trap 'printf "FAILED at setup line %s. Log: %s/%s.log\n" "$LINENO" "$state_dir" "$mode"' ERR
[[ "$(uname -m)" == x86_64 ]] || { printf 'Requires Linux x86-64\n'; exit 1; }
driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n 1 | tr -d '[:space:]')"
[[ "$driver_version" == 610.57.04 ]] || { printf 'Driver is %s, expected 610.57.04; no changes made.\n' "$driver_version"; exit 1; }
if [[ "$mode" == test ]]; then
    cd "$node_dir"
    printf '%s\n' 'TEST 1/3: Wine worker --help (no DLSS evaluation).'
    "$py_exec" -u check_runtime.py --help-worker
    printf '%s\n' 'TEST 2/3: upstream production NR feature-18 probe.'
    "$py_exec" -u check_runtime.py --probe-nr --adapter 0
    printf '%s\n' 'TEST 3/3: actual native-resolution image and 2-frame SDR video.'
    "$py_exec" -u "$kit_dir/smoke_gpu.py" --node "$node_dir" --output "$state_dir/gpu-test"
    printf 'TESTS COMPLETED. Inspect outputs and logs: %s\n' "$state_dir/gpu-test"
    exit 0
fi
[[ "$(id -u)" == 0 ]] || { printf 'Install mode requires root for apt packages.\n'; exit 1; }
. /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]] || { printf 'This installer targets Ubuntu 24.04 only.\n'; exit 1; }
"$py_exec" -c 'import shutil,sys; p=sys.argv[1]; free=shutil.disk_usage(p).free; print("Free space: %.1f GiB"%(free/2**30)); assert free>=8*2**30,"Reserve at least 8 GiB for dependencies, archives, extracted runtimes and prefix"' "$state_dir"
printf '%s\n' 'STEP 1/5: Ubuntu user-space prerequisites (no NVIDIA packages).'
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    curl ca-certificates unzip tar xz-utils zstd cabextract xvfb xauth \
    wine64 wine winetricks libvulkan1 vulkan-tools ffmpeg
mkdir -p "$state_dir/downloads" "$state_dir/runtime"
"$py_exec" -u "$kit_dir/fetch_runtime.py" --node "$node_dir" --cache "$state_dir/downloads"
"$py_exec" -u "$kit_dir/configure_local.py" check-node --node "$node_dir"
fetch() {
    "$py_exec" -u "$kit_dir/fetch_asset.py" "$1" "$2" "$3"
}
printf '%s\n' 'STEP 2/5: pinned GE-Proton11-6 Wine and graphics libraries.'
proton_url="${V2D_PROTON_URL:-https://github.com/GloriousEggroll/proton-ge-custom/releases/download/GE-Proton11-6/GE-Proton11-6-x86_64.tar.gz}"
fetch "$proton_url" \
    "$state_dir/downloads/GE-Proton11-6-x86_64.tar.gz" \
    '659f8d71f2f78659340120b20c1c5a1464aa138939332a1376dea22f6d2dc2e4'
if [[ ! -f "$state_dir/runtime/.proton-extracted" ]]; then
    tar -xzf "$state_dir/downloads/GE-Proton11-6-x86_64.tar.gz" -C "$state_dir/runtime"
    touch "$state_dir/runtime/.proton-extracted"
fi
proton_files="$(find "$state_dir/runtime" -path '*/files/bin/wine' -print -quit)"
[[ -n "$proton_files" ]] || { printf 'Cannot find GE-Proton files/bin/wine\n'; exit 1; }
proton_files="${proton_files%/bin/wine}"
printf '%s\n' 'STEP 3/5: extract matching NVIDIA bridge; never run nvidia-installer.'
fetch 'https://download.nvidia.com/XFree86/Linux-x86_64/610.57.04/NVIDIA-Linux-x86_64-610.57.04.run' \
    "$state_dir/downloads/NVIDIA-Linux-x86_64-610.57.04.run" \
    'b2e935c66b83bb00c0c857bc8e0ee0fd52de9286b40c9cc1eec29a7ce7eb116d'
driver_dir="$state_dir/runtime/NVIDIA-Linux-x86_64-610.57.04"
if [[ ! -f "$state_dir/runtime/.driver-extracted" ]]; then
    sh "$state_dir/downloads/NVIDIA-Linux-x86_64-610.57.04.run" --extract-only --target "$driver_dir"
    touch "$state_dir/runtime/.driver-extracted"
fi
printf '%s\n' 'STEP 4/5: Microsoft native x64 shader compiler (Winetricks SDK recipe).'
fetch 'https://download.microsoft.com/download/B/0/C/B0C80BA3-8AD6-4958-810B-6882485230B5/standalonesdk/Installers/61d57a7a82309cd161a854a6f4619e52.cab' \
    "$state_dir/downloads/d3dcompiler_x64.cab" 'f736e161547095bb8d98c636b85fdfeb4070fefeee3c3745db3ce88f6eb1d9de'
mkdir -p "$state_dir/runtime/compiler"
cabextract -L -F 'fil3585cb2ea5db13cc0838f8d06b5c9679' -d "$state_dir/runtime/compiler" "$state_dir/downloads/d3dcompiler_x64.cab"
compiler="$state_dir/runtime/compiler/fil3585cb2ea5db13cc0838f8d06b5c9679"
printf '%s\n' 'STEP 5/5: configure repository node and its managed Wine prefix.'
"$py_exec" -u "$kit_dir/configure_local.py" configure --node "$node_dir" --state "$state_dir" \
    --proton "$proton_files" --compiler "$compiler" --driver-dir "$driver_dir"
cd "$node_dir"
"$py_exec" -u check_runtime.py
printf 'INSTALL COMPLETE. Run: bash "%s/setup_ubuntu24.sh" test\n' "$kit_dir"
