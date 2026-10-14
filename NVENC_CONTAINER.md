# Optional NVENC workaround inside a Linux container

This is a separate experimental workaround for native Linux FFmpeg. It does
not run FFmpeg through Wine. Keep the existing NVIDIA driver and Wine setup.
The target user's 610.57.04 container loads matching CUDA/NVENC/NVDEC libraries
and NVENC API 13.1, but `OpenEncodeSessionEx` returns unsupported device (2).
The revised PCI mapping passed the user's actual H.264 NVENC test on this
Ubuntu 24.04 x86_64 Docker / RTX 5090 / Linux 610.57.04 target on 2026-10-14.
Both 256x256 and 1296x720 outputs contained 12 H.264 frames, encoder exit code
0, and all frames decoded successfully. The source SHA-256 was
`4b068c35c74f2ee62a175cbad46d79d11138eeecd6aa1fa24a581a06c500e687`.
This confirms two short standalone GPU encoding tests. Joint DLSSG + NVENC
execution inside ComfyUI, audio, HDR, HEVC, AV1 and long video remain pending.
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
and NVIDIA control character device. GPU IDs remain opaque: the shim calls
`GET_PCI_INFO` from NVIDIA's 610.57.04 headers, then matches the complete PCI
**domain, bus and slot** against `/proc/driver/nvidia/gpus/*/information` to
obtain the Linux `Device Minor`. It does not equate RM `deviceInstance` with a
Linux minor, nor derive PCI addresses by shifting GPU IDs. All returned IDs
must resolve without ambiguity before filtering; otherwise the original list
is retained. Immutable caches initialize before FFmpeg starts its threads.
Device existence is checked against actual character-device numbers and a
successful open, rather than a pathname alone.

### Target failure captured on 2026-10-14

The first target trial did **not** pass. It exposed an error in the initial
mapping: RM `deviceInstance=2` belonged to GPU ID `0x6a00`, while Linux device
minor 2 belonged to PCI `0000:94:00.0`, GPU ID `0x9400`. The shim mistakenly
retained `0x6a00`, causing CUDA initialization to return `CUDA_ERROR_NO_DEVICE`
in the scoped FFmpeg process. The revised shim uses `GET_PCI_INFO` and a
regression reproduces this eight-GPU ordering, including duplicate bus numbers
across PCI domains. The revised source has now passed both standalone H.264 GPU encoding tests:
logs show correct retention of GPU ID `0x9400`, PCI `0000:94:00`, Linux minor
2, filtering the host list from eight IDs to the one accessible GPU. FFmpeg
reports `h264_nvenc`, both 12-frame files were decoded, and `results.json`
reports `PASS`. These are user-provided target results, separate from CI.
Raw host logs, host identifiers, and GPU UUIDs are not published.

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
  defines `GET_ATTACHED_IDS` and `GET_PCI_INFO`.
- `test_nvenc_container.py` exercises synthetic RM calls, ioctl ABI boundaries,
  the reported broadcast-instance/minor mismatch, opaque IDs,
  unknown/ambiguous PCI mappings, kernel-error preservation and wrapper scoping.
  These checks do not execute NVENC or grant access to real GPUs.
