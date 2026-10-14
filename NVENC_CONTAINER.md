# 影栈（CineStack）DLSS · 容器 GPU 编码指南

本指南介绍原生 Linux FFmpeg 的可选 NVENC GPU 枚举修复，以及 ComfyUI 的持久化启动配置。此组件为实验性部署工具，适用于出现 `OpenEncodeSessionEx: unsupported device (2)` 的部分 NVIDIA 容器环境。

工具保留现有 NVIDIA 驱动与 Wine 配置。修复仅在 FFmpeg 包装器中加载，不向 ComfyUI、PyTorch 或 Wine 全局注入 `LD_PRELOAD`。

## 使用前提

- Linux x86-64，容器中可访问 NVIDIA GPU 与配套编码运行库。
- `/usr/bin/ffmpeg` 和 `/usr/bin/ffprobe` 可用，FFmpeg 包含 `h264_nvenc`。
- 有 `gcc` 与 C 标准库开发文件用于构建组件。
- 使用持久化磁盘保存运行文件与配置。

该修复针对显卡枚举问题，不覆盖所有 NVENC 初始化故障。缺少编码运行库、GPU 不支持编码或容器设备未开放时，需先解决对应部署问题。

## 构建与验收

在节点目录及 ComfyUI 的 Python 环境中运行。Ubuntu / Debian 可先安装编译依赖：

```bash
sudo apt-get install -y --no-install-recommends gcc libc6-dev
python nvenc_container.py test
```

已有系统管理权限的终端可直接运行包管理命令。工具按源码哈希创建独立运行目录，打印 FFmpeg 包装器、测试输出和报告位置。可通过 `V2D_NVENC_STATE` 指定存储基目录；动态库路径不能含空白或冒号。

测试选择容器可见的 CUDA 索引 `0`，在 256×256 与 1296×720 两个尺寸各编码 12 帧 H.264，检查编码退出状态、尺寸、帧数和编解码器，并完整解码输出。仅 `results.json` 显示 `PASS` 才可进入部署阶段。

测试不含音轨，也不覆盖 HDR、HEVC、AV1、长视频或完整 ComfyUI 工作流。单独编译成功不能视为 GPU 编码验收。

## 接入 ComfyUI

工具为使用 `DLSS_FFMPEG_PATH` / `DLSS_FFPROBE_PATH` 的节点提供编码器选择。其中独立的 `ComfyUI-NVIDIA-DLSS-Frame-Interpolation` 节点支持这两个变量；其他节点需核对各自的 FFmpeg 选择规则。

通过测试后，用报告打印的包装器路径配置启动环境：

```bash
export DLSS_FFMPEG_PATH="/path/to/verified-runtime/ffmpeg"
export DLSS_FFPROBE_PATH=/usr/bin/ffprobe
```

重启 ComfyUI 后，在插帧节点选择 `H.264 (NVIDIA NVENC)`，先运行短素材并检查输出。节点导入时会读取编码器路径，因此修改环境后需要重启。

`python nvenc_container.py path` 也可获取路径，但会重新构建运行文件。持久化安装完成后，避免无必要地重建；文件指纹变化时需要重新测试并部署。

## 持久化手动与开机启动

`install_nvenc_startup.py` 复用已有运行文件和匹配的两尺寸 `PASS` 报告，检查来源及文件指纹，然后部署启动文件。它不安装系统包、更换驱动、重建组件或自动重启服务。

以下示例适用于平台确实在启动时执行 `/start.d/*.sh` 的容器。其他平台应将 `--boot-script` 指向其实际启动入口，并按平台规则注册执行。

在节点目录执行，替换 ComfyUI 安装路径：

```bash
COMFYUI_DIR="/path/to/ComfyUI"
python install_nvenc_startup.py \
  --comfyui "$COMFYUI_DIR" \
  --boot-script /start.d/comfyui.sh \
  --check
```

`--check` 仅验证与显示部署计划。确认路径对应部署位置后执行：

```bash
python install_nvenc_startup.py \
  --comfyui "$COMFYUI_DIR" \
  --boot-script /start.d/comfyui.sh
```

| 文件 | 用途 |
| --- | --- |
| ComfyUI 安装目录的 `start.sh` | 新启动时读取持久化 FFmpeg 配置 |
| ComfyUI 安装目录的 `stop.sh` | 按实例身份停止服务 |
| ComfyUI 安装目录的 `comfyui-runtime.json` | 保存编码器路径和包装器 / 动态库指纹 |
| `--boot-script` 指定的入口 | 调用同一份 `start.sh` |

### 设置 Python 解释器

通用部署应显式指定 ComfyUI 的 Python 可执行文件。手动启停使用：

```bash
export V2D_COMFY_PYTHON="/path/to/comfyui-python"
"$COMFYUI_DIR/stop.sh" && "$COMFYUI_DIR/start.sh"
```

开机时不会继承当前终端导出的变量。请在生成的开机入口中、`exec bash` 行之前，设置实际解释器；端口等自定义设置也应在此保存：

```bash
export V2D_COMFY_PYTHON="/path/to/comfyui-python"
```

修改后用 `bash -n /path/to/boot-script.sh` 检查语法，并确认平台会执行该入口。重新运行安装器可能重写开机入口，随后应重新核对自定义设置。

### 重启验收

手动启动与容器启动均应检查：

1. 启动日志中的 FFmpeg 路径是否为已验证的包装器。
2. ComfyUI 是否正常监听配置的端口。
3. 短工作流是否成功使用 `H.264 (NVIDIA NVENC)`，保存文件是否能完整解码。
4. 容器重启后再次运行上述检查，确认持久化配置与平台启动入口均生效。

ComfyUI 日志为安装目录中的 `comfy.log`。开机日志位置由平台决定；使用 `/start.d` 的平台请查阅其日志规则。工作流编码器选择仍需保存在工作流中，安装器不改写已有工作流。

## 备份与恢复

已有文件会备份到 ComfyUI 安装目录的 `node_backups/nvenc-startup-*`，清单记录对应原文件。备份位于开机执行目录之外。重复安装相同内容不会重复修改，普通写入失败会恢复已更改的文件。

新启动时包装器与动态库必须存在且指纹一致。缺失或改变会阻止启动并报错；停止服务不依赖运行库校验。

临时返回系统 FFmpeg 时，启动前设置：

```bash
export DLSS_FFMPEG_PATH=/usr/bin/ffmpeg
export DLSS_FFPROBE_PATH=/usr/bin/ffprobe
```

同时将工作流编码器调整为普通 `H.264`。需要完整恢复时按备份清单还原文件；不要将备份启动脚本放入会自动执行的目录。

运行文件和配置需放在容器保留的存储中。镜像重装或替换磁盘后需要重新部署。

## 实现与许可

组件过滤 GPU 枚举响应，使 FFmpeg 使用容器已可访问的设备。它通过 NVIDIA `GET_PCI_INFO` 查询完整 PCI 地址，再匹配系统公开的设备信息；映射不明确时保留原响应。组件不增加 GPU 访问权限。

`nvenc_fix.c` 基于 [flexgrip/nvidia-gpu-enumeration](https://github.com/flexgrip/nvidia-gpu-enumeration) 的固定提交 `1475091ae9e8fb23cc22d565adf932a488d00bcd`，遵循 [GPL-3.0](NVENC_FIX_LICENSE.txt)。NVIDIA 控制接口依据 [GPU 控制头文件](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/610.57.04/src/common/sdk/nvidia/inc/ctrl/ctrl0000/ctrl0000gpu.h)。

相关兼容性讨论见 [NVIDIA device plugin issue #1282](https://github.com/NVIDIA/k8s-device-plugin/issues/1282)。讨论与自动化回归不替代部署环境中的实际编码验证。
