# video2dlssnr v1.4.1 — Linux/Wine 适配试验版

本包新增 Linux x86-64 的 Wine 启动与数据传输适配，Windows 继续直接启动 EXE。原版节点 ID、端口、参数和工作流连接保持兼容。原版 EXE 和 DLSS/NR DLL 均未修改。部署与最新实机问题处理以 [README](README.md) 为准。

**验证范围：本地传输、安装文件与运行库获取测试通过；用户 RTX 5090 / Linux 610.57.04 已通过生产 NR feature 18 探测及原生分辨率单图输出。视频在旧 shader compiler 的光流编译阶段失败，同时确认 stdout 日志混入。新版编译器及文件传输修复仍待实机复测，不能视为完整 Linux 视频 DLSS 已验证版本。**

## 改了什么

- 三个节点统一通过 `wine_runtime.py` 启动程序。Linux 使用 Wine，Windows 直接执行。
- 图像的输入、输出及 DLL 路径按指定 prefix 的实际 `dosdevices` 盘符映射转为 Windows 路径；支持空格、中文和引号，不经过 shell。
- 视频继续使用独立的 stdin/stdout 二进制管道；stderr 单独读取。输出不足、输出多余、程序退出失败都会报错。
- 保存的 Wine 配置只传给本节点的工作子进程，不修改 ComfyUI/PyTorch 及其他节点的环境。
- 整个处理任务有超时保护，默认 1800 秒，可调。超时终止本次 Wine 进程组，不执行 `wineserver -k`。
- 修复上游 `DLSS NR Runtime Check` 多传一个参数、无法进入程序的错误。

## 实机上仍可能卡在哪里

适配解决的是 Python 到 Windows 程序的启动和传输。下列问题仍须实机确认：

1. NVIDIA Vulkan、NGX 驱动桥、NVAPI/D3D12 是否可用；仅有 CUDA 不能替代这些组件。
2. 原版 C++ 程序 `CheckDriverForNr()` 要求 NVIDIA 驱动编号不低于 616.56。它通过 DXGI 的 UMD 版本解码编号，Wine 的报告值可能不满足检查，或无法正确反映 Linux 驱动。此次没有绕过该检查，也不能把 Windows 版本号当作 Linux 驱动安装指令。
3. NR 的 feature 18 初始化/求值、NVOF 后端及不同 GPU 的运行结果。上游使用直接 NR forwarder 路线，不使用另一个 DLSS 节点的 RenoDX/ReShade 路线；不能把那个仓库的专用 ReShade DLL 当成这里的修复。
4. 无桌面服务器、Docker、HDR 及视觉质量尚未实机验证。正常启动或 `--help` 返回成功不证明 NR 已执行。

## 1. 放入 ComfyUI

把本包中的整个 `video2dlssnr` 文件夹放到：

```text
ComfyUI/custom_nodes/ComfyUI-DLSS-NeuralRender-Linux/
    __init__.py
    nodes.py
    wine_runtime.py
    bin/video2dlssnr.exe
    bin/nvngx_dlss.dll
    bin/nvngx_dlssnr.dll
    bin/nvngx.dll_dlssnr.dll
```

若已有原版，先停止 ComfyUI，将原版备份到 `custom_nodes` 之外，再放入适配版。不要同时保留两份同名节点。轻量补丁 ZIP 只含 Python、文档与测试，需覆盖到已有 **v1.4.1 完整节点文件夹**；完整包可直接解压。

## 2. 准备系统组件

目标系统需要 Linux x86-64、兼容 NVIDIA RTX、正常的 NVIDIA 图形/Vulkan/NGX 驱动，以及 Wine、VKD3D-Proton、DXVK 和 DXVK-NVAPI。建议使用同一套 GE-Proton 提供的 Wine 和图形库，避免混用版本。

检查机器信息：

```bash
cat /etc/os-release
uname -m
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
vulkaninfo --summary
```

Wine/GE-Proton 和系统包的安装命令需按发行版选定。下方配置借鉴另一个仓库已公开的 Wine/Proton 环境，**并未在 video2dlssnr 上验证其 GPU 兼容性**。

## 3. 配置独立 prefix

在 Bash 中使用机器上的真实路径。`V2D_PROTON_FILES` 应指向包含 `bin/wine` 和 `lib/wine` 的 `files` 目录；`WINEPREFIX` 应为本节点新建的独立目录，不要使用其他程序的 prefix。

```bash
export V2D_PROTON_FILES="/absolute/path/to/GE-Proton/files"
export VIDEO2DLSSNR_WINE="$V2D_PROTON_FILES/bin/wine"
export WINESERVER="$V2D_PROTON_FILES/bin/wineserver"
export WINEPREFIX="$HOME/.local/share/video2dlssnr-wine"
```

确认 Wine 路径可执行，并且 prefix 尚未存在，然后初始化：

```bash
test -x "$VIDEO2DLSSNR_WINE" && test ! -e "$WINEPREFIX" && "$VIDEO2DLSSNR_WINE" wineboot -u
```

若该命令失败，请先确认失败原因。它不会给已有 prefix 重新初始化，避免覆盖已有图形 DLL；之后先检查 `drive_c/windows/system32` 和 `dosdevices` 是否已建立。

将同一 GE-Proton 的 x86-64 图形库装到专用 prefix 中。以下使用 GNU `cp`，在 prefix 初始化成功、相关来源文件实际存在后执行：

```bash
export V2D_SYSTEM32="$WINEPREFIX/drive_c/windows/system32"
cp --remove-destination "$V2D_PROTON_FILES/lib/wine/dxvk/x86_64-windows/"{dxgi,d3d11}.dll "$V2D_SYSTEM32/"
cp --remove-destination "$V2D_PROTON_FILES/lib/wine/vkd3d-proton/x86_64-windows/"{d3d12,d3d12core}.dll "$V2D_SYSTEM32/"
cp --remove-destination "$V2D_PROTON_FILES/lib/wine/nvapi/x86_64-windows/"{nvapi64,nvofapi64}.dll "$V2D_SYSTEM32/"
```

来源目录如不存在，需确认该 GE-Proton 包的布局，不要从其他版本任意拼装 DLL。任何复制错误都应先解决，再进行下一步。

在此 prefix 中安装新版微软原生 x64 shader compiler。当前自动部署固定为 **10.0.28000.2705**，微软官方 CAB 下载、校验和提取步骤见 `setup_ubuntu24.sh`；旧版 Winetricks 的 `d3dcompiler_47` 可能仍下载 2013 年的 6.3.9600 DLL，在光流着色器报 `X3676`，不能仅凭文件名确认兼容。

若提示缺少 MSVCP140/VCRUNTIME140，再在该 prefix 中安装 `vcrun2022`。Winetricks 失败必须检查原因，不能按“已安装”处理。

## 4. NVIDIA 驱动桥与环境

找到**当前 Linux NVIDIA 驱动**配套的 `_nvngx.dll` 及 `libnvidia-ngx.so`。某些发行版将前者放在 `/usr/lib/nvidia/wine/_nvngx.dll`，其他发行版路径可能不同。

在节点文件夹中，用实际路径为程序建立驱动桥链接：

```bash
cd /your/ComfyUI/custom_nodes/ComfyUI-DLSS-NeuralRender-Linux
export V2D_DRIVER_BRIDGE="/actual/driver/path/_nvngx.dll"
test -f "$V2D_DRIVER_BRIDGE" && ln -s "$V2D_DRIVER_BRIDGE" bin/_nvngx.dll
```

若链接已存在，检查现有目标，不覆盖它。`libnvidia-ngx.so` 必须能被 Linux 动态加载器找到。不要拿不配套的 Windows 驱动 DLL 代替 Linux 驱动桥。

设置工作子进程环境：

```bash
export WINEDLLOVERRIDES='dxgi,d3d11,d3d12,d3d12core,nvapi64,nvofapi64,d3dcompiler_47=n,b'
export DXVK_ENABLE_NVAPI=1
export DXVK_CONFIG='dxgi.hideNvidiaGpu = False'
export WINE_HEAP_DELAY_FREE=1
export WINE_HEAP_ZERO_MEMORY=1
export WINEDEBUG=-all
```

多显卡机器可额外设置 `DXVK_FILTER_DEVICE_NAME` 和 `VKD3D_FILTER_DEVICE_NAME` 为同一张 NVIDIA GPU 的完整名称，并在节点 `adapter` 中选择对应枚举索引；不能仅靠 `CUDA_VISIBLE_DEVICES` 推断 Wine 选择了哪张卡。

## 5. 保存节点本地配置

当前终端已设置上面的真实路径后，在节点文件夹运行：

```bash
python3 - <<'PY'
import json, os
from pathlib import Path
from wine_runtime import ENV_KEYS
settings = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
Path('linux-runtime.json').write_text(json.dumps({
    'version': 1, 'timeout_seconds': 1800, 'environment': settings
}, indent=2))
PY
```

这样可以照常启动 ComfyUI，无需把 Wine 环境导出给所有节点。`linux-runtime.example.json` 是字段示例，包含占位路径，不会自动生效。机器迁移后需要重新保存配置。

如果不保存配置，则从当前配置好的终端启动 ComfyUI，使节点继承这些环境变量。长视频可以增大配置中的 `timeout_seconds`，或在未设置该字段时使用 `VIDEO2DLSSNR_TIMEOUT`（单位秒，必须是有限正数）。

## 6. 分层验收

先激活目标 ComfyUI 自己的 Python 环境，在节点文件夹执行：

```bash
python check_runtime.py
python check_runtime.py --help-worker
python check_runtime.py --probe-nr --adapter 0
```

- 第一条只检查配置、盘符映射和 PE 文件头，不创建 GPU 功能。
- 第二条实际启动 EXE 的帮助页，验证 Wine 到程序的调用，仍不验证 DLSS。
- 第三条使用上游生产 NR 路线探测 feature 18。检查 GPU/驱动、`Init_Ext`、`CreateFeature 18`、`EvaluateFeature 18` 与退出状态。脚本不启用 `--probe-core`，也不改动原版驱动检查。

随后重启 ComfyUI，运行 `DLSS NR Runtime Check`。它处理真实的两帧测试数据，成功时检查输出尺寸和日志；若显示失败，请保留全部诊断文本。

最终分别运行：

1. `Load Image → DLSS Neural Rendering (Image) → Save Image`，先用 `scale=1` 检查 NR 图像处理，再验证放大。
2. `Load Video → DLSS Neural Rendering (Video) → Save Video`，先用短 SDR 视频、`scale=1`、`motion_engine=lk`，再测试放大、`auto/nvof` 和 HDR。

检查实际图片/视频是否可解码，分辨率、帧数、帧率、音轨是否符合输入及设置，再比较视觉效果。Runtime Check 或 NR 探测成功均不替代完整图像/视频验收。该节点不添加帧。

遇到超时、`NGX initialization failed`、driver too old、CreateFeature/EvaluateFeature 错误时，先保留输出日志及机器查询结果；这些故障可能属于 GPU/驱动/Wine，而不是文件路径或 Python 传输。调试 Wine 时可在保存配置中暂设 `WINEDEBUG=+seh,+loaddll`，日志仍走 stderr。

## 本地回归测试

### Ubuntu 24.04 无桌面 Docker 部署

本仓库根目录的 `setup_ubuntu24.sh` 针对 RTX 5090 / 610.57.04 环境，提供 `install` 和 `test` 两个入口。驱动 `.run` 包只解压，用于取得匹配的 `_nvngx.dll`；使用 GE-Proton11-6 的 Wine 和图形库。

无桌面调用设置 `VIDEO2DLSSNR_XVFB=1`，每个 Wine 工作进程单独由 `xvfb-run` 启动。需要 `xvfb` 和 `xauth`；`WINE_GRAPHICS_DRIVER=x11` 也可保存进本节点配置。Xvfb 满足 Wine 的显示连接，DLSS 仍使用筛选后的 NVIDIA GPU。该支持已通过模拟包装器的管道回归测试，真实容器结果需运行部署包的 `test` 验收。

当前代码共有 13 项传输/配置回归测试；其中新增两项验证 Xvfb 包装后的二进制流和缺少包装器时的错误处理。

```bash
python -m unittest discover -s . -p 'test_*.py' -v
```

需要已有 ComfyUI 环境中的 NumPy、PyTorch、Pillow；没有额外 Python 依赖。测试用 Python 脚本模拟 Wine 和 EXE，验证实际子进程、图像路径、SDR/HDR 管道、错误输出、超时与工作进程组清理，以及 Windows 命令构建。**这些测试不是 NVIDIA/Wine/DLSS 实机测试。**

## 来源

- 上游版本与用途：[video2dlssnr v1.4.1](https://github.com/DaniilSokolyuk/video2dlssnr/releases/tag/v1.4.1)，完整发布包中的 EXE/DLL 保留原样。
- C++ 启动和二进制协议：[上游源码](https://github.com/DaniilSokolyuk/video2dlssnr/tree/v1.4.1/src)。驱动门槛见 `src/nr.cpp` 的 `CheckDriverForNr`。
- Wine 环境参考：[ComfyUI-NVIDIA-DLSS-Frame-Interpolation Linux 文档](https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation/blob/main/docs/linux.md)。该仓库的成功验证不等于本包已通过 GPU 验收。
- NVIDIA API 暴露方式：[NVIDIA Linux 驱动说明](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/gaming.html)。
- 上游 MIT 许可保存在 `LICENSE`；NVIDIA 运行库遵循其自身条款。
