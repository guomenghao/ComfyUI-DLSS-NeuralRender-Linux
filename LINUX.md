# 影栈（CineStack）DLSS · Linux 配置指南

本指南用于在 Linux x86-64 上配置影栈 DLSS ComfyUI 节点的实验性 Wine 后端。图像与视频处理由上游 Windows 工作程序执行，ComfyUI 与 PyTorch 继续使用 Linux 环境。

产品功能与快速安装入口见 [README](README.md)，验收方法见 [功能验收指南](VALIDATION.md)。

## 安装方式

**自动安装**：`setup_ubuntu24.sh` 当前仅接受 Ubuntu 24.04、RTX 5090 与 NVIDIA 驱动 610.57.04。它使用固定版本运行库、SHA-256 校验和专用 Wine prefix，保留现有驱动与 PyTorch。预留至少 8 GiB 空间，并使用有系统依赖安装权限的账号执行：

```bash
bash setup_ubuntu24.sh install
bash setup_ubuntu24.sh test
```

在节点目录中使用 ComfyUI 的 Python 环境运行以上命令。`V2D_PYTHON` 可指定解释器；`V2D_STATE` 可指定存储安装状态、下载缓存与测试输出的持久化目录。

**手动配置**：适用于需要自行准备 Wine 与图形组件的部署。其他硬件、驱动和发行版组合需独立验收，手动配置不代表兼容性已得到保证。

## 系统组件

需要可用的 NVIDIA Vulkan / NGX 图形运行库，以及 Wine、VKD3D-Proton、DXVK、DXVK-NVAPI、FFmpeg 和 FFprobe。建议使用同一套 GE-Proton 提供的 Wine 与图形库。

安装前确认 `nvidia-smi` 可识别显卡，`vulkaninfo --summary` 可枚举真实 NVIDIA GPU。只有 CUDA 计算支持不足以满足 DLSS 的图形组件要求。

无桌面环境需要 Xvfb 和 Xauth。直接使用 GE-Proton Wine 时还需满足其系统共享库依赖；自动安装配置包含 32 位 glibc 加载器依赖。

## 运行库

在节点目录执行：

```bash
python fetch_runtime.py
```

也可使用已下载的发布包：

```bash
python fetch_runtime.py --archive /path/to/video2dlssnr-comfyui.zip
```

工具只安装通过固定校验的上游运行库。请勿将原版 Python 节点覆盖到本项目目录，或同时加载原版与影栈节点。

### 下载与缓存

- 运行文件支持断点续传；中断后保留缓存再重试。
- `V2D_RUNTIME_ARCHIVE` 指定本地上游 ZIP，`V2D_RUNTIME_DIR` 指定已有运行库的 `bin` 目录。
- `V2D_RUNTIME_URL` 与 `V2D_PROTON_URL` 可指定同一固定资产的下载来源；文件仍必须通过原有校验。
- 不要将下载缓存或运行配置存放在容器重启后会清空的目录。

## 手动配置 Wine

以下使用 Bash。将占位路径替换为实际安装位置，并创建专用于本节点的 prefix。

```bash
export V2D_PROTON_FILES="/path/to/GE-Proton/files"
export VIDEO2DLSSNR_WINE="$V2D_PROTON_FILES/bin/wine"
export WINESERVER="$V2D_PROTON_FILES/bin/wineserver"
export WINEPREFIX="/path/to/persistent/cinestack-dlss-prefix"
```

`V2D_PROTON_FILES` 需包含 `bin/wine` 与配套图形库。确认解释器可执行、prefix 尚未存在，再初始化；无桌面环境使用：

```bash
test -x "$VIDEO2DLSSNR_WINE" && test ! -e "$WINEPREFIX" && \
xvfb-run -a "$VIDEO2DLSSNR_WINE" wineboot -u
```

有显示服务时可直接执行 Wine。初始化后检查 `drive_c/windows/system32` 与 `dosdevices`；已有 prefix 不应重复初始化。

将同一运行套件的 x86-64 DLL 复制到专用 prefix。先确认来源目录存在；如有已有文件，先备份，再执行：

```bash
V2D_SYSTEM32="$WINEPREFIX/drive_c/windows/system32"
cp --remove-destination "$V2D_PROTON_FILES/lib/wine/dxvk/x86_64-windows/"{dxgi,d3d11}.dll "$V2D_SYSTEM32/"
cp --remove-destination "$V2D_PROTON_FILES/lib/wine/vkd3d-proton/x86_64-windows/"{d3d12,d3d12core}.dll "$V2D_SYSTEM32/"
cp --remove-destination "$V2D_PROTON_FILES/lib/wine/nvapi/x86_64-windows/"{nvapi64,nvofapi64}.dll "$V2D_SYSTEM32/"
```

不同版本的目录布局可能不同，请按安装套件核对。光流着色器需要兼容的微软原生 x64 `d3dcompiler_47.dll`；自动安装配置固定使用版本 **10.0.28000.2705**，官方下载地址、哈希与提取方式见 [安装脚本](setup_ubuntu24.sh)。如工作程序提示缺少 MSVCP140 / VCRUNTIME140，再为该 prefix 配置相应运行库。

## NVIDIA 驱动桥

使用与当前 Linux NVIDIA 驱动配套的 `_nvngx.dll` 与 `libnvidia-ngx.so`。其中后者应能被 Linux 动态加载器找到。节点的 `bin/_nvngx.dll` 可链接到匹配的驱动桥：

```bash
COMFYUI_DIR="/path/to/ComfyUI"
cd "$COMFYUI_DIR/custom_nodes/ComfyUI-DLSS-NeuralRender-Linux"
V2D_DRIVER_BRIDGE="/path/to/matching/_nvngx.dll"
test -f "$V2D_DRIVER_BRIDGE" && test ! -e bin/_nvngx.dll && \
test ! -L bin/_nvngx.dll && ln -s "$V2D_DRIVER_BRIDGE" bin/_nvngx.dll
```

已有链接应先核对来源。不要混用不配套的 Windows 驱动 DLL。自动安装配置仅解压 NVIDIA 归档获取驱动桥，不运行驱动安装器。

## 节点配置

设置工作程序所需参数：

```bash
export WINEDLLOVERRIDES='dxgi,d3d11,d3d12,d3d12core,nvapi64,nvofapi64,d3dcompiler_47=n,b'
export DXVK_ENABLE_NVAPI=1
export DXVK_CONFIG='dxgi.hideNvidiaGpu = False'
export WINE_HEAP_DELAY_FREE=1
export WINE_HEAP_ZERO_MEMORY=1
export WINEDEBUG=-all
export VIDEO2DLSSNR_VIDEO_TRANSPORT=files
```

无桌面部署额外设置：

```bash
export VIDEO2DLSSNR_XVFB=1
export WINE_GRAPHICS_DRIVER=x11
```

多显卡部署可使用 `DXVK_FILTER_DEVICE_NAME` 与 `VKD3D_FILTER_DEVICE_NAME` 指定同一 NVIDIA GPU 的名称，再核对节点的 `adapter` 索引。CUDA 的显卡索引与 Wine 的枚举结果不能直接等同。

在配置好的终端中进入节点目录，将允许的环境字段保存到 `linux-runtime.json`：

```bash
python - <<'PY'
import json
import os
from pathlib import Path
from wine_runtime import ENV_KEYS
settings = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
Path('linux-runtime.json').write_text(json.dumps({
    'version': 1,
    'timeout_seconds': 1800,
    'environment': settings,
}, indent=2))
PY
```

保存配置只用于本节点的工作子进程。`linux-runtime.example.json` 为字段示例，不会自动生效。迁移安装目录或运行库后需重新配置绝对路径。

| 选项 | 用途 |
| --- | --- |
| `VIDEO2DLSSNR_WINE` | 指定 Wine 可执行文件，不能填写整条 shell 命令 |
| `WINEPREFIX` | 已初始化的专用 prefix 绝对路径 |
| `VIDEO2DLSSNR_XVFB` | `1` 为工作程序启用 Xvfb，`0` 为关闭 |
| `VIDEO2DLSSNR_VIDEO_TRANSPORT` | `files` 隔离日志与原始帧，`pipe` 使用二进制管道 |
| `timeout_seconds` | 配置中的任务超时秒数，必须为有限正数 |
| `VIDEO2DLSSNR_TIMEOUT` | 未在配置中指定超时时使用的环境默认值 |

文件传输需要临时磁盘容纳输入和输出 RGBA 帧，处理结束后清理本次临时目录。批处理参数不能包含双引号或换行，临时目录还不能包含 `%` 或 `!`；必要时设置 `TMPDIR=/tmp`。文件模式的进度在处理完成时更新。

## 验收与维护

按 [功能验收指南](VALIDATION.md) 完成配置检查、NR 功能探测和实际素材测试。帮助页成功与运行库加载成功不能替代 GPU 处理验收。

上游 Windows 驱动检查通过 DXGI UMD 版本判断；Wine 中的报告值可能不同于实际 Linux 驱动版本。不要据此替换 Linux 驱动，或将 Windows 驱动版本门槛视为 Linux 安装要求。

启停、任务取消、内存与临时磁盘规划见 [运行维护指南](CODE_REVIEW.md)。原生 Linux FFmpeg 的可选 GPU 编码修复见 [NVENC 指南](NVENC_CONTAINER.md)。
