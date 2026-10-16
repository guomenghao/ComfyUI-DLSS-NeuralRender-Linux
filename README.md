# 影栈（CineStack）DLSS · ComfyUI 节点

**将 NVIDIA DLSS Super Resolution 与 Neural Rendering 接入 ComfyUI 图像和视频工作流。**

影栈（CineStack）DLSS 提供图像增强、视频时序处理和运行环境检查节点，可用于 AI 生成素材的后期处理。通过节点参数调整神经渲染风格、细节、色彩与输出尺寸，将处理结果继续交给 ComfyUI 的预览、保存和后续制作节点。

本项目基于 [video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr) 构建，提供 Linux / Wine 适配与部署工具，并保留 Windows 原生执行方式。Linux 后端目前为实验性功能。

## 0.2.0：逐帧视频处理

`DLSSNR_Video` 新增末尾参数 `output_mode`，旧工作流无需重建节点：

- `auto`（默认）：只连接 VIDEO 输出时，逐帧输入同一个 DLSS 工作进程，再逐帧编码为文件 VIDEO；不会为整片创建超分浮点张量。
- `file_video`：强制文件输出；IMAGE 输出被阻断。适合 SR → NR → 补帧 → SaveVideo。
- `images_and_video`：兼容旧 IMAGE 消费者，保留一个完整结果张量；输入转换和 worker 输出仍逐帧进行。

与影栈 H3 0.6.9 配合时，普通 CreateVideo 接线可以继续用；更低内存的完整链路是「全部导出＋滚动生成」的 `final_video` → DLSS `video`。一采 `first_pass_video` 单独接 SaveVideo。详见 [内存与文件交接](STREAMING.md)。

## 产品功能

- **图像处理**：处理单张图像或图像批次，提供 Neural Rendering 与 DLSS 超分相关参数。
- **视频处理**：接收 ComfyUI `VIDEO` 或 `IMAGE` 帧批次，使用光流运动信息进行时序神经渲染，输出图像帧与 `VIDEO`。
- **风格与细节控制**：提供风格、预设、强度、局部结构、局部色调、皮肤、细节和色彩参数。
- **输出尺寸控制**：支持缩放比例及指定宽高，便于衔接后期制作流程。
- **运行环境检查**：查看工作程序及运行库信息，辅助安装验收与问题定位。
- **Linux 部署支持**：提供 Wine 运行配置、无桌面运行支持，以及可选的容器 NVENC 编码修复和启停工具。

视频节点保持输入帧数。提高帧率的 DLSS Frame Generation 需使用独立的插帧节点；本项目的 Neural Rendering 节点负责图像与视频画面处理。

## 节点一览

安装后，在 ComfyUI 的 `video2dlssnr` 分类中查找以下节点：

| 节点名称 | 用途 | 输入 / 输出 |
| --- | --- | --- |
| **DLSS Neural Rendering (Image)** | 单图及图像批次处理 | `IMAGE` → `IMAGE` |
| **DLSS Neural Rendering (Video)** | 视频时序处理 | `VIDEO` 或 `IMAGE` → `IMAGE`、`VIDEO` |
| **DLSS NR Runtime Check** | 运行环境检查 | 输出 `STRING` |

保留上游节点 ID：`DLSSNR_Image`、`DLSSNR_Video`、`DLSSNR_RuntimeInfo`，方便衔接已有工作流。原版 `video2dlssnr` 与本项目使用相同 ID，请避免同时加载。

## 安装要求

- 已安装 ComfyUI，并使用其实际运行的 Python 环境执行安装命令。
- NVIDIA RTX 显卡及可用的配套图形运行库。
- Linux 需要 Wine 与 NVIDIA Vulkan 支持；无桌面环境可使用 Xvfb。
- 运行库首次安装需要网络连接和足够的磁盘空间。

**自动安装配置的兼容范围**：当前 `setup_ubuntu24.sh` 仅适用于 Ubuntu 24.04、x86-64、RTX 5090 与 NVIDIA 驱动 610.57.04。其他 Linux 组合请参阅 [Linux 配置指南](LINUX.md)，需分别验证兼容性。此限制属于安装配置的适用范围。

Windows 直接调用上游工作程序，无需 Wine。上游要求 NVIDIA RTX 与 Windows 驱动 616.56 或更高版本；本项目尚未完成 Windows GPU 回归验证。

## 快速安装

### 1. 下载节点

将以下 `COMFYUI_DIR` 替换为自己的 ComfyUI 安装目录，然后执行：

```bash
COMFYUI_DIR="/path/to/ComfyUI"
cd "$COMFYUI_DIR/custom_nodes"
git clone https://github.com/guomenghao/ComfyUI-DLSS-NeuralRender-Linux.git
cd ComfyUI-DLSS-NeuralRender-Linux
```

若已安装原版 `video2dlssnr`，先停止相关任务，将原版目录移到 `custom_nodes` 之外保存，避免重复节点冲突。

### 2. 安装运行库

**Linux：符合自动安装配置要求时**，在 ComfyUI 的 Python 环境中执行：

```bash
bash setup_ubuntu24.sh install
bash setup_ubuntu24.sh test
```

安装系统依赖需要管理员权限，建议预留至少 8 GiB 空间。安装器使用固定版本与 SHA-256 校验的运行文件，配置专用 Wine 环境；保留现有 NVIDIA 驱动和 PyTorch。以 `INSTALL COMPLETE` 确认安装完成，并检查测试结果后再使用工作流。

**Windows：**

```bash
python fetch_runtime.py
```

该命令需要 PATH 中可用的 `curl`，用于下载并校验上游运行库。

### 3. 重启 ComfyUI

重启后搜索上述节点，即可加入工作流。

## 使用示例

**图像处理：**

```text
Load Image → DLSS Neural Rendering (Image) → Save Image
```

**视频处理：**

```text
Load Video → DLSS Neural Rendering (Video) → Save Video
```

建议先以短 SDR 样本、`scale=1` 及 `motion_engine=lk` 完成运行检查，再逐项调整风格与输出尺寸。

| 常用参数 | 说明 |
| --- | --- |
| `style` / `preset` | 选择神经渲染风格和预设 |
| `intensity` | 调整处理强度 |
| `detail` / `color` | 调整细节与色彩处理比例 |
| `scale` | 未指定宽高时的输出缩放比例 |
| `width` / `height` | 指定输出尺寸；只指定一项时按输入比例计算另一项 |
| `motion_engine` | 选择视频光流引擎 |
| `adapter` | 选择工作程序使用的显卡索引 |

连接 `VIDEO` 输入时，代码会将音频与帧率传递到 `VIDEO` 输出；使用 `IMAGE` 帧批次时，通过 `images_fps` 设置输出帧率元数据，帧数保持不变。HDR、音轨与超分路径请使用实际素材单独验收。

## Linux 部署工具

仓库提供运行库下载与校验、Wine 配置、GPU 冒烟测试，以及 `start.sh` / `stop.sh` 启停脚本。启停脚本放置在包含 `main.py` 的 ComfyUI 安装目录，可通过 `V2D_COMFY_PYTHON` 指定解释器。

对于出现 `OpenEncodeSessionEx: unsupported device (2)` 的 Linux 容器，另提供可选的 FFmpeg GPU 枚举修复。通过实际 NVENC 编码测试后，可将已验证的运行配置接入手动启动和容器开机入口。详见 [容器 NVENC 与持久化启动指南](NVENC_CONTAINER.md)。

Linux 运行方式与配置参数详见 [Linux 配置指南](LINUX.md)。

## 兼容性与验证范围

Linux 后端已完成原生分辨率单图、短 SDR 视频及 Lucas–Kanade 光流处理验证；可选 NVENC 工具已完成短 H.264 硬件编码与解码验证。

超分放大、HDR、NVOF、音轨、长视频及不同硬件组合仍需分别验证。独立编码测试与 CI 不代表完整 ComfyUI 工作流已通过验收，画面质量也需根据实际素材评估。

## 更新

在本节点目录执行：

```bash
git status --short
git pull --ff-only
```

更新前保留本地修改与运行配置。若更新涉及 Linux 运行库或配置，重新执行安装及相应测试，然后重启 ComfyUI。

## 开源与致谢

感谢 [Daniil Sokolyuk / video2dlssnr](https://github.com/DaniilSokolyuk/video2dlssnr) 提供上游实现，以及 Wine、GE-Proton、DXVK、VKD3D-Proton 和 DXVK-NVAPI 等项目。

主体代码遵循 [MIT License](LICENSE)，保留上游版权声明。可选 NVENC 修复组件遵循 [GPL-3.0](NVENC_FIX_LICENSE.txt)。NVIDIA 及其他第三方运行库按各自许可使用，不随本仓库源码分发。

影栈（CineStack）为本节点的产品品牌。本项目独立开发与维护，未获得 NVIDIA 官方背书；NVIDIA、DLSS 及相关名称归其各自权利人所有。
