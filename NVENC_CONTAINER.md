# Optional NVENC workaround inside a Linux container

This is a separate experimental workaround for native Linux FFmpeg. It does
not run FFmpeg through Wine. Keep the existing NVIDIA driver and Wine setup.
The target user's 610.57.04 container loads matching CUDA/NVENC/NVDEC libraries
and NVENC API 13.1, but `OpenEncodeSessionEx` returns unsupported device (2).
GPU H.264 encoding with this workaround remains **unverified on that target**.
The same target has already passed short native DLSSG interpolation with CPU
H.264 encoding at 1296x720, 30 to 60 FPS, 12 input and 24 output frames.

The workaround intercepts the attached GPU ID query and filters the response
to devices already accessible in the container. It grants no additional GPU
access. `nvenc_fix.c` is based on
[flexgrip/nvidia-gpu-enumeration](https://github.com/flexgrip/nvidia-gpu-enumeration)
commit `1475091ae9e8fb23cc22d565adf932a488d00bcd`. That component uses GPL-3.0;
its license is retained in `NVENC_FIX_LICENSE.txt`. The other project source
retains its existing license.

Local changes check the complete ioctl code, RM control ABI, successful status,
and NVIDIA control character device. GPU ID lookup uses the documented V2
structure from NVIDIA's 610.57.04 headers. If this fails, the upstream heuristic
PCI lookup is accepted only when every ID can be mapped without ambiguity;
unknown or duplicate matches retain the original response. NVIDIA documents
GPU IDs as opaque; this fallback is still an experimental heuristic, not an
ABI guarantee. Immutable caches initialize before FFmpeg starts its threads.
Device existence is checked against actual character-device numbers and a
successful open, rather than a pathname alone.

## Build and run the GPU test

From this repository, on Linux x86_64 with `/usr/bin/ffmpeg` and `ffprobe`:

```bash
apt-get install -y --no-install-recommends gcc libc6-dev
/root/miniconda3/bin/python nvenc_container.py test
```

Run after other GPU tasks finish. The helper builds into a source-hash directory
under `~/.local/share/comfyui-nvenc-fix/` (or `V2D_NVENC_STATE`). It generates an
FFmpeg-only wrapper. `LD_PRELOAD` is set inside that wrapper, not globally in
ComfyUI, PyTorch, Wine, or shell startup files. Original driver libraries,
device nodes, prefix, and startup scripts are not edited.

The test explicitly selects CUDA ordinal 0, the single GPU visible on this
target. It attempts 12-frame H.264 NVENC outputs at 256x256 and 1296x720,
verifies size/frame count/codec through ffprobe, then decodes every frame on
the CPU. Logs and `results.json` remain in the build directory. Only `PASS`
confirms these two short hardware encoding tests; audio, HDR, HEVC, AV1 and
long video are not covered. A successful compile is not a GPU test.

## Connect to Frame Interpolation after PASS

Obtain the wrapper path using `python nvenc_container.py path`. The helper prints
only that path on success. Stop the current ComfyUI using its control script,
then set `DLSS_FFMPEG_PATH` to the wrapper for the new ComfyUI start. Keep
`DLSS_FFPROBE_PATH=/usr/bin/ffprobe`. The Frame Interpolation node resolves
these paths when imported, so a restart is required. Select `H.264 (NVIDIA NVENC)`
and test a short workflow before long work. This setting targets the separate
`ComfyUI-NVIDIA-DLSS-Frame-Interpolation` node; other nodes may use different
FFmpeg selection rules.

To roll back, restart ComfyUI with `DLSS_FFMPEG_PATH=/usr/bin/ffmpeg` and
`DLSS_FFPROBE_PATH=/usr/bin/ffprobe`, and return to plain `H.264` if NVENC still
fails. This optional helper does not change automatic startup configuration.

## Sources and validation

- [NVIDIA device plugin issue #1282](https://github.com/NVIDIA/k8s-device-plugin/issues/1282)
  includes a user's successful LD_PRELOAD report and a maintainer's discussion.
- [610.57.04 GPU control header](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/610.57.04/src/common/sdk/nvidia/inc/ctrl/ctrl0000/ctrl0000gpu.h)
  defines `GET_ATTACHED_IDS` and `GET_ID_INFO_V2`.
- `test_nvenc_container.py` exercises synthetic RM calls, ioctl ABI boundaries,
  unknown/ambiguous mappings, kernel-error preservation and wrapper scoping.
  These checks do not execute NVENC or grant access to real GPUs.
